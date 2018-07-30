#!python3
import torch
import numpy as np
import torch.nn as nn
from torch.nn import DataParallel
from torch.nn import MSELoss
from pycocotools.coco import COCO
from pycocotools.cocoeval import COCOeval

import math
import torch.utils.model_zoo as model_zoo

import pose.models as models
import pose.datasets as datasets
import pose.utils.config as config
from pose.utils.transforms import fliplr_pts

from pose.utils.misc import adjust_learning_rate

from pose.models.bayproj import AutoCorrProj
from .baseexperiment import BaseExperiment

import cv2
import re

FACTOR = 4

class Experiment(BaseExperiment):
    def init(self):
        self.num_parts = datasets.mscoco.NUM_PARTS
        use_pretrained = (config.resume is not None)
        self.model = DataParallel(BayProj(self.hparams["model"]["out_shape"][::-1], self.num_parts, pretrained=use_pretrained).cuda())
        self.criterion = MSELoss().cuda()
        _, extra_parameters = zip(*list(filter(lambda x: x[1].requires_grad and re.match(r"^(.+\.)?extra_mod(\..+)?$", x[0]) is not None, self.model.named_parameters())))
        _, normal_parameters = zip(*list(filter(lambda x: x[1].requires_grad and re.match(r"^(.+\.)?extra_mod(\..+)?$", x[0]) is None, self.model.named_parameters())))

        self.optimizer = torch.optim.Adam([
                {"params": normal_parameters},
                {"params": extra_parameters, "lr": self.hparams["learning_rate"] * 0.2, "init_lr": self.hparams["learning_rate"] * 0.2}
            ],
            lr=self.hparams["learning_rate"],
            weight_decay=self.hparams['weight_decay'])
        self.cur_lr = self.hparams["learning_rate"]

        self.coco = COCO("data/mscoco/person_keypoints_train2014.json")

        self.train_dataset = datasets.COCOSinglePose("data/mscoco/images",
                                               self.coco,
                                               "data/mscoco/sp_split_3.pth",
                                               "data/mscoco/mean_std_3.pth",
                                               True,
                                               img_res=self.hparams["model"]["inp_shape"],
                                               ext_border=self.hparams["dataset"]["ext_border"],
                                               kpmap_res=self.hparams["model"]["out_shape"],
                                               keypoint_res=self.hparams["model"]["out_shape"],
                                               kpmap_sigma=self.hparams["model"]["gaussian_kernels"],
                                               scale_factor=self.hparams["dataset"]["scale_factor"],
                                               rot_factor=self.hparams["dataset"]["rotate_factor"],
                                               trans_factor=self.hparams["dataset"]["translation_factor"])

        self.val_dataset = datasets.COCOSinglePose("data/mscoco/images",
                                             self.coco,
                                             "data/mscoco/sp_split_3.pth",
                                             "data/mscoco/mean_std_3.pth",
                                             False,
                                             img_res=self.hparams["model"]["inp_shape"],
                                             ext_border=self.hparams["dataset"]["ext_border"],
                                             kpmap_res=self.hparams["model"]["out_shape"],
                                             keypoint_res=self.hparams["model"]["out_shape"],
                                             kpmap_sigma=self.hparams["model"]["gaussian_kernels"],
                                             scale_factor=self.hparams["dataset"]["scale_factor"],
                                             rot_factor=self.hparams["dataset"]["rotate_factor"],
                                             trans_factor=self.hparams["dataset"]["translation_factor"])

        self.train_collate_fn = datasets.COCOSinglePose.collate_function
        self.test_collate_fn = datasets.COCOSinglePose.collate_function
        self.worker_init_fn = datasets.mscoco.worker_init
        self.print_iter_start = " | "

    def evaluate(self, preds, step):
        def _summarize(eval_result, params, ap, iou_thr=None, area_rng="all", max_dets=100, title=None):
            type_str = "AP" if ap==1 else "AR"
            if title is None:
                iou_str = "{:0.2f}-{:0.2f}".format(params.iouThrs[0], params.iouThrs[-1]) \
                    if iou_thr is None else "{:0.2f}".format(iou_thr)
                title = "{:<9}_{:>6s}_{:>3d}".format(iou_str, area_rng, max_dets)

            aind = [i for i, aRng in enumerate(params.areaRngLbl) if aRng == area_rng]
            mind = [i for i, mDet in enumerate(params.maxDets) if mDet == max_dets]
            if ap == 1:
                # dimension of precision: [TxRxKxAxM]
                s = eval_result["precision"]
                # IoU
                if iou_thr is not None:
                    t = np.where(iou_thr == params.iouThrs)[0]
                    s = s[t]
                s = s[:,:,:,aind,mind]
            else:
                # dimension of recall: [TxKxAxM]
                s = eval_result["recall"]
                if iou_thr is not None:
                    t = np.where(iou_thr == params.iouThrs)[0]
                    s = s[t]
                s = s[:,:,aind,mind]
            if len(s[s>-1])==0:
                mean_s = -1
            else:
                mean_s = np.mean(s[s>-1])
            config.tb_writer.add_scalars("{}/{}".format(config.exp_name, type_str), {title: mean_s}, step)
            return mean_s

        image_ids = preds["image_index"]
        ans = preds["annotate"]
        if len(ans) > 0:
            coco_dets = self.coco.loadRes(ans)
            coco_eval = COCOeval(self.coco, coco_dets, "keypoints")
            coco_eval.params.imgIds = list(image_ids)
            coco_eval.params.catIds = [1]
            coco_eval.evaluate()
            coco_eval.accumulate()

            _summarize(coco_eval.eval, coco_eval.params, 1, title="avg", max_dets=20)
            _summarize(coco_eval.eval, coco_eval.params, 1, title="i50", max_dets=20, iou_thr=.5)
            _summarize(coco_eval.eval, coco_eval.params, 1, title="i75", max_dets=20, iou_thr=.75)
            _summarize(coco_eval.eval, coco_eval.params, 1, title="med", max_dets=20, area_rng="medium")
            _summarize(coco_eval.eval, coco_eval.params, 1, title="lar", max_dets=20, area_rng="large")
            _summarize(coco_eval.eval, coco_eval.params, 0, title="avg", max_dets=20)
            _summarize(coco_eval.eval, coco_eval.params, 0, title="i50", max_dets=20, iou_thr=.5)
            _summarize(coco_eval.eval, coco_eval.params, 0, title="i75", max_dets=20, iou_thr=.75)
            _summarize(coco_eval.eval, coco_eval.params, 0, title="med", max_dets=20, area_rng="medium")
            _summarize(coco_eval.eval, coco_eval.params, 0, title="lar", max_dets=20, area_rng="large")

            coco_eval.summarize()
        else:
            config.tb_writer.add_scalars("{}/{}".format(config.exp_name, "AP"), {"avg": -1.}, step)
            config.tb_writer.add_scalars("{}/{}".format(config.exp_name, "AP"), {"i50": -1.}, step)
            config.tb_writer.add_scalars("{}/{}".format(config.exp_name, "AP"), {"i75": -1.}, step)
            config.tb_writer.add_scalars("{}/{}".format(config.exp_name, "AP"), {"med": -1.}, step)
            config.tb_writer.add_scalars("{}/{}".format(config.exp_name, "AP"), {"lar": -1.}, step)
            config.tb_writer.add_scalars("{}/{}".format(config.exp_name, "AR"), {"avg": -1.}, step)
            config.tb_writer.add_scalars("{}/{}".format(config.exp_name, "AR"), {"i50": -1.}, step)
            config.tb_writer.add_scalars("{}/{}".format(config.exp_name, "AR"), {"i75": -1.}, step)
            config.tb_writer.add_scalars("{}/{}".format(config.exp_name, "AR"), {"med": -1.}, step)
            config.tb_writer.add_scalars("{}/{}".format(config.exp_name, "AR"), {"lar": -1.}, step)

            print("No points")

    def epoch_start(self, epoch):
        self.cur_lr = adjust_learning_rate(self.optimizer, epoch, self.hparams['learning_rate'], self.hparams['schedule'], self.hparams['lr_gamma'])

    def iter_process(self, epoch_ctx, batch, is_train, progress):
        image_ids = batch["img_index"].tolist()
        img = batch["img"]
        det_maps_gt = batch["keypoint_map"]
        transform_mat = batch["img_transform"]
        img_flipped = batch["img_flipped"]
        img_ori_size = batch["img_ori_size"]
        keypoint = batch["keypoint"]
        batch_size = img.size(0)

        det_map_gt_vars = [dm.cuda() for dm in det_maps_gt]
        output_vars = self.model(img)

        loss = 0.
        for ilabel, (outv, gtv) in enumerate(zip(output_vars, det_map_gt_vars)):
            # if ilabel < len(det_map_gt_vars) - 1:
            #     gtv *= (keypoint[:, :, 2] > 1.1).float().view(-1, self.num_parts, 1, 1).cuda()
            if ilabel < len(det_map_gt_vars) - 1:
                loss += ((outv - gtv).pow(2) * \
                    (keypoint[:, :, 2] != 1).float().view(-1, self.num_parts, 1, 1).cuda()).mean().sqrt()
            else:
                loss += (outv - gtv).pow(2).mean().sqrt()

        if (loss.data != loss.data).any():
            import pdb; pdb.set_trace()

        if not is_train or config.vis:
            pred, score = parse_map(output_vars[-1])
            pred_affined = pred.copy()
            for samp_i in range(batch_size):
                pred_affined[samp_i, :, :2] = kpt_affine(pred_affined[samp_i, :, :2] * FACTOR, np.linalg.pinv(transform_mat[samp_i])[:2])
                if img_flipped[samp_i]:
                    pred_affined[samp_i] = fliplr_pts(pred_affined[samp_i], datasets.mscoco.FLIP_INDEX, width=img_ori_size[samp_i, 0].item())
            ans = generate_ans(image_ids, pred_affined, score)
        else:
            pred = None
            ans = None

        if config.vis:
            import matplotlib.pyplot as plt
            img_restored = np.ascontiguousarray(self.train_dataset.restore_image(img.data.cpu().numpy())[..., ::-1])
            
            if False:
                nrows = int(np.sqrt(float(batch_size)))
                ncols = (batch_size + nrows - 1) // nrows
                fig, axes = plt.subplots(nrows, ncols, squeeze=False)
                for ax in axes.flat:
                    ax.axis("off")
                for i in range(batch_size):
                    draw_img = img_restored[i].copy()
                    for j in range(self.num_parts):
                        pt = pred[i, j]
                        if pt[2] > 0:
                            cv2.circle(draw_img, (int(pt[0] * FACTOR), int(pt[1] * FACTOR)), radius=2, color=(0, 0, 255), thickness=-1)
                    axes.flat[i].imshow(draw_img[..., ::-1])

                plt.show()

            if True:
                for i in range(batch_size):
                    nrows = 3; ncols = 6
                    for i_out in range(len(output_vars)):
                        pred_resized = batch_resize((output_vars[i_out][i].data.cpu().numpy().clip(0, 1) * 255).round().astype(np.uint8) , img.size()[-2:])
                        
                        fig, axes = plt.subplots(nrows, ncols, squeeze=False)
                        fig.suptitle("%d" % (i_out,))
                        for ax in axes.flat:
                            ax.axis("off")
                        for j in range(self.num_parts):
                            ax = axes.flat[j]
                            draw_img = cv2.addWeighted(img_restored[i], 1, cv2.applyColorMap(pred_resized[j, :, :, None], cv2.COLORMAP_HOT), 0.5, 0)
                            ax.imshow(draw_img[..., ::-1])
                            ax.set_title(datasets.mscoco.PART_LABELS[j])
                    plt.show()

            # if loss.item() > 0.1:
            #     import pdb; pdb.set_trace()
        epoch_ctx.add_scalar("loss", loss.item(), progress["iter_len"])

        result = {
            "loss": loss,
            "index": batch["index"],
            "save": None,
            "pred": {"image_index": image_ids, "annotate": ans}
        }

        return result

class BayProj(nn.Module):
    def __init__(self, output_shape, num_points, pretrained=True):
        super(BayProj, self).__init__()
        self.resnet50 = resnet50(pretrained=pretrained, extra_mod=[
            None,
            None,
            AutoCorrProj,
            None])
        self.global_net = globalNet([2048, 1024, 512, 256], output_shape, num_points)

    def forward(self, x):
        res_out = self.resnet50(x)
        global_re, global_out = self.global_net(res_out)
        return global_out

def conv3x3(in_planes, out_planes, stride=1):
    "3x3 convolution with padding"
    return nn.Conv2d(in_planes, out_planes, kernel_size=3, stride=stride,
                     padding=1, bias=False)

class BasicBlock(nn.Module):
    expansion = 1

    def __init__(self, inplanes, planes, stride=1, downsample=None):
        super(BasicBlock, self).__init__()
        self.conv1 = conv3x3(inplanes, planes, stride)
        self.bn1 = nn.BatchNorm2d(planes)
        self.relu = nn.ReLU(inplace=True)
        self.conv2 = conv3x3(planes, planes)
        self.bn2 = nn.BatchNorm2d(planes)
        self.downsample = downsample
        self.stride = stride

    def forward(self, x):
        residual = x

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)

        if self.downsample is not None:
            residual = self.downsample(x)

        out += residual
        out = self.relu(out)

        return out

class Bottleneck(nn.Module):
    expansion = 4

    def __init__(self, inplanes, planes, stride=1, downsample=None, extra_mod=None):
        super(Bottleneck, self).__init__()
        self.conv1 = nn.Conv2d(inplanes, planes, kernel_size=1, bias=False)
        self.bn1 = nn.BatchNorm2d(planes)
        self.conv2 = nn.Conv2d(planes, planes, kernel_size=3, stride=stride,
                               padding=1, bias=False)
        self.bn2 = nn.BatchNorm2d(planes)
        self.conv3 = nn.Conv2d(planes, planes * 4, kernel_size=1, bias=False)
        self.bn3 = nn.BatchNorm2d(planes * 4)
        self.relu = nn.ReLU(inplace=True)
        self.downsample = downsample
        self.stride = stride
        if extra_mod is not None:
            self.extra_mod = extra_mod(inplanes, inplanes, 32, corr_kernel_size=(7, 7), corr_stride=(3, 3))
        else:
            self.extra_mod = None

    def forward(self, x):
        residual = x

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)
        out = self.relu(out)

        out = self.conv3(out)
        out = self.bn3(out)

        if self.downsample is not None:
            residual = self.downsample(x)
        elif self.extra_mod is not None:
            if self.extra_mod.out_channels < x.size(1):
                residual = torch.cat([x[:, :self.extra_mod.out_channels] + self.extra_mod(x), x[:, self.extra_mod.out_channels:]], dim=1)
            else:
                residual = x + self.extra_mod(x)

        out += residual
        out = self.relu(out)

        return out


class ResNet(nn.Module):

    def __init__(self, block, layers, num_classes=1000, extra_mod=[None, None, None, None]):
        self.inplanes = 64
        super(ResNet, self).__init__()
        self.conv1 = nn.Conv2d(3, 64, kernel_size=7, stride=2, padding=3,
                               bias=False)
        self.bn1 = nn.BatchNorm2d(64)
        self.relu = nn.ReLU(inplace=True)
        self.maxpool = nn.MaxPool2d(kernel_size=3, stride=2, padding=1)
        self.layer1 = self._make_layer(block, 64, layers[0], extra_mod=extra_mod[0])
        self.layer2 = self._make_layer(block, 128, layers[1], stride=2, extra_mod=extra_mod[1])
        self.layer3 = self._make_layer(block, 256, layers[2], stride=2, extra_mod=extra_mod[2])
        self.layer4 = self._make_layer(block, 512, layers[3], stride=2, extra_mod=extra_mod[3])

        for mod_name, m in self.named_modules():
            if re.match(r"^(.+\.)?extra_mod(\..+)?$", mod_name):
                continue
            if isinstance(m, nn.Conv2d):
                n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
                m.weight.data.normal_(0, math.sqrt(2. / n))
            elif isinstance(m, nn.BatchNorm2d):
                m.weight.data.fill_(1)
                m.bias.data.zero_()

    def _make_layer(self, block, planes, blocks, stride=1, extra_mod=None):
        downsample = None
        if stride != 1 or self.inplanes != planes * block.expansion:
            downsample = nn.Sequential(
                nn.Conv2d(self.inplanes, planes * block.expansion,
                          kernel_size=1, stride=stride, bias=False),
                nn.BatchNorm2d(planes * block.expansion),
            )

        layers = []
        layers.append(block(self.inplanes, planes, stride, downsample))
        self.inplanes = planes * block.expansion
        for i in range(1, blocks):
            if extra_mod is not None and i == 1:
                layers.append(block(self.inplanes, planes, extra_mod=extra_mod))
            else:
                layers.append(block(self.inplanes, planes))

        return nn.Sequential(*layers)

    def forward(self, x):
        x = self.conv1(x)
        x = self.bn1(x)
        x = self.relu(x)
        x = self.maxpool(x)

        x1 = self.layer1(x)
        x2 = self.layer2(x1)
        x3 = self.layer3(x2)
        x4 = self.layer4(x3)

        return [x4, x3, x2, x1]

def resnet50(pretrained=False, **kwargs):
    """Constructs a ResNet-50 model.
    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
    """
    model = ResNet(Bottleneck, [3, 4, 6, 3], **kwargs)
    if pretrained:
        from collections import OrderedDict
        state_dict = model.state_dict()
        pretrained_state_dict = torch.load("pretrained/resnet50-19c8e357.pth")
        for k, v in pretrained_state_dict.items():
            if k not in state_dict:
                continue
            state_dict[k] = v
        model.load_state_dict(state_dict)
    return model


def resnet101(pretrained=False, **kwargs):
    """Constructs a ResNet-101 model.
    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
    """
    model = ResNet(Bottleneck, [3, 4, 23, 3], **kwargs)
    if pretrained:
        from collections import OrderedDict
        state_dict = model.state_dict()
        pretrained_state_dict = torch.load("pretrained/resnet101-19c8e357.pth")
        for k, v in pretrained_state_dict.items():
            if k not in state_dict:
                continue
            state_dict[k] = v
        model.load_state_dict(state_dict)
    return model

class globalNet(nn.Module):
    def __init__(self, channel_settings, output_shape, num_class):
        super(globalNet, self).__init__()
        self.channel_settings = channel_settings
        laterals, upsamples, predict = [], [], []
        for i in range(len(channel_settings)):
            laterals.append(self._lateral(channel_settings[i]))
            predict.append(self._predict(output_shape, num_class))
            if i != len(channel_settings) - 1:
                upsamples.append(self._upsample())
        self.laterals = nn.ModuleList(laterals)
        self.upsamples = nn.ModuleList(upsamples)
        self.predict = nn.ModuleList(predict)

        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
                m.weight.data.normal_(0, math.sqrt(2. / n))
                if m.bias is not None:
                    m.bias.data.zero_()
            elif isinstance(m, nn.BatchNorm2d):
                m.weight.data.fill_(1)
                m.bias.data.zero_()

    def _lateral(self, input_size):
        layers = []
        layers.append(nn.Conv2d(input_size, 256,
            kernel_size=1, stride=1, bias=False))
        layers.append(nn.BatchNorm2d(256))
        layers.append(nn.ReLU(inplace=True))

        return nn.Sequential(*layers)

    def _upsample(self):
        layers = []
        layers.append(torch.nn.Upsample(scale_factor=2, mode='bilinear', align_corners=True))
        layers.append(torch.nn.Conv2d(256, 256,
            kernel_size=1, stride=1, bias=True))
        layers.append(nn.BatchNorm2d(256))

        return nn.Sequential(*layers)

    def _predict(self, output_shape, num_class):
        layers = []
        layers.append(nn.Conv2d(256, 256,
            kernel_size=1, stride=1, bias=False))
        layers.append(nn.BatchNorm2d(256))
        layers.append(nn.ReLU(inplace=True))

        layers.append(nn.Conv2d(256, num_class,
            kernel_size=3, stride=1, padding=1, bias=False))
        layers.append(nn.Upsample(size=output_shape, mode='bilinear', align_corners=True))
        layers.append(nn.BatchNorm2d(num_class))

        return nn.Sequential(*layers)

    def forward(self, x):
        global_fms, global_outs = [], []
        for i in range(len(self.channel_settings)):
            if i == 0:
                feature = self.laterals[i](x[i])
            else:
                feature = self.laterals[i](x[i]) + up
            global_fms.append(feature)
            if i != len(self.channel_settings) - 1:
                up = self.upsamples[i](feature)
            feature = self.predict[i](feature)
            global_outs.append(feature)

        return global_fms, global_outs

def parse_map(det_map, thres=0.1, factor=4):
    det_map = det_map.detach()
    if det_map.is_cuda:
        det_map = det_map.cpu()
    det_map = det_map.numpy()
    num_batch = det_map.shape[0]
    num_part = det_map.shape[1]
    height = det_map.shape[2]
    width = det_map.shape[3]

    pred = np.zeros((num_batch, num_part, 3), dtype=np.float32)
    score = np.zeros((num_batch, num_part), dtype=np.float32)
    for sample_i in range(num_batch):
        for part_i in range(num_part):
            loc = det_map[sample_i, part_i].argmax().item()
            y = loc // width
            x = loc % width
            score_sp = det_map[sample_i, part_i, y, x]
            # TODO: test always 1 and always store score
            if score_sp > thres:
                pred[sample_i, part_i, 2] = 1
                score[sample_i, part_i] = score_sp
            if det_map[sample_i, part_i, y, max(0, x-1)] < det_map[sample_i, part_i, y, min(width-1, x+1)]:
                off_x = 0.25
            else:
                off_x = -0.25
            if det_map[sample_i, part_i, max(0, y-1), x] < det_map[sample_i, part_i, min(height-1, y+1), x]:
                off_y = 0.25
            else:
                off_y = -0.25
            pred[sample_i, part_i, 0] = x + 0.5 + off_x
            pred[sample_i, part_i, 1] = y + 0.5 + off_y
    return pred, score

def generate_ans(image_ids, preds, scores):
    ans = []
    for sample_i in range(len(preds)):
        image_id = image_ids[sample_i]

        val = preds[sample_i]
        score = scores[sample_i].mean()
        if val[:, 2].max()>0:
            tmp = {'image_id':int(image_id), "category_id": 1, "keypoints": [], "score":float(score)}
            # # p: average detected locations
            # p = val[val[:, 2] > 0][:, :2].mean(axis = 0)
            # for j in val:
            #     if j[2]>0.:
            #         tmp["keypoints"] += [float(j[0]), float(j[1]), 1]
            #     else:
            #         # TRICK: for not detected points, place them at the average point
            #         tmp["keypoints"] += [float(p[0]), float(p[1]), 0]
            tmp["keypoints"] = val.ravel().tolist()
            ans.append(tmp)
    return ans

def kpt_affine(kpt, mat):
    kpt = np.array(kpt)
    shape = kpt.shape
    kpt = kpt.reshape(-1, 2)
    return np.dot( np.concatenate((kpt, kpt[:, 0:1]*0+1), axis = 1), mat.T ).reshape(shape)

def batch_resize(im, new_shape):
    assert isinstance(new_shape, tuple) and len(new_shape) == 2 and isinstance(new_shape[0], int) and isinstance(new_shape[1], int)
    im_pre_shape = im.shape[:-2]
    im_post_shape = im.shape[-2:]
    if im_post_shape == new_shape:
        return im
    im = im.reshape((-1,) + im_post_shape)
    return np.array([cv2.resize(im[i], (new_shape[1], new_shape[0])) for i in range(im.shape[0])]).reshape(im_pre_shape + new_shape)

if __name__ == "__main__":
    def test_main():
        from ruamel.yaml import YAML
        import pose.utils.config as config
        import importlib

        exp_name = "baybase"

        with open('experiments/config.yaml', 'r') as f:
            conf = YAML(typ='safe').load(f)
            conf_data = conf["default"]
            config.__dict__.update(conf_data.items())

        config.exp_name = exp_name
        with open("experiments/hparams.yaml", "r") as f:
            hparams = YAML(typ="safe").load(f)[exp_name]

        config.checkpoint = config.checkpoint.format(**{'exp': exp_name, 'id': hparams['id']})
        if config.resume is not None:
            config.resume = config.resume.format(**{'exp': exp_name, 'id': hparams['id']})

        exp_module = importlib.import_module('experiments.' + exp_name)
        exp = exp_module.Experiment(hparams)

        exp.train_dataset.debug = True

        train_loader = torch.utils.data.DataLoader(
            exp.train_dataset,
            collate_fn=exp.train_collate_fn,
            batch_size=exp.hparams['train_batch'],
            num_workers=0,
            shuffle=True,
            pin_memory=True,
            drop_last=exp.train_drop_last if hasattr(exp, "train_drop_last") else False)

        for batch in train_loader:
            continue

    test_main()