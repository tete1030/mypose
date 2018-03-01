from __future__ import print_function, absolute_import
import torch
from torch.utils.data.dataloader import default_collate

import pose.models as models
import pose.datasets as datasets
from pose.utils.transforms import fliplr_chwimg, fliplr_map
from pose.utils.evaluation import PR_multi
import pose.utils.config as config
from pose.utils.misc import adjust_learning_rate

from pycocotools.coco import COCO

import numpy as np
import copy

try:
    profile
except NameError:
    profile = lambda func: func

"""
1. Detect person location / head
2. Generate maps of part location and embedding
3. Input into network, output vector fields
4. Update part location
5. A: If iterating counter less than M, goto 2 (Stack Sharing/Non-sharing)
   B: If stable, goto 2 (Stack Non-sharing)

# Keypoint map fusion
- Could banlance semantic for image and keypoint maps

# Loss
- from loose to tight / equal

Input:
    A:
        image map: 128
        fuse map: 128
        keypoint map: 2 * 17
    B:
        image/fuse map: 256
        keypoint map: 2 * 17
    C:
        image/fuse map: 256
        keypoint map: 256 (after fusion)

Output:
    offset field: 2 * 17
"""

FACTOR = 4
INP_RES = 256
OUT_RES = 64

class Experiment(object):
    """Stretch Experiment
    """

    def __init__(self, hparams):
        self.hparams = hparams
        self.num_parts = datasets.mscoco.NUM_PARTS

        self.model = torch.nn.ModuleList([
            torch.nn.DataParallel(
                models.PosePre(img_dim=3,
                               hg_dim=self.hparams["model"]["hg_dim"],
                               bn=self.hparams["model"]["bn"]).cuda()),
            torch.nn.DataParallel(
                models.PoseNet(inp_dim=self.num_parts*2, out_dim=self.num_parts*2,
                               hg_dim=self.hparams["model"]["hg_dim"],
                               bn=self.hparams["model"]["bn"]).cuda())])

        self.criterion = models.PoseDisLoss().cuda()

        self.optimizer = torch.optim.Adam(list(self.model.parameters()),
                                          lr=self.hparams["learning_rate"],
                                          weight_decay=self.hparams["weight_decay"])

        # Only used when train and valid dataset are all from train2014
        self.coco = COCO("data/mscoco/person_keypoints_train2014.json")

        self.train_dataset = datasets.COCOPose("data/mscoco/images",
                                               self.coco,
                                               "data/mscoco/split.pth",
                                               "data/mscoco/mean_std.pth",
                                               train=True,
                                               single_person=False,
                                               inp_res=INP_RES,
                                               out_res=OUT_RES,
                                               generate_map=False)

        self.val_dataset = datasets.COCOPose("data/mscoco/images",
                                             self.coco,
                                             "data/mscoco/split.pth",
                                             "data/mscoco/mean_std.pth",
                                             train=False,
                                             single_person=False,
                                             inp_res=INP_RES,
                                             out_res=OUT_RES,
                                             generate_map=False)

        self.train_pose_mgr = models.PoseManager(self.hparams["train_batch"],
                                                 self.num_parts,
                                                 (INP_RES, INP_RES),
                                                 self.hparams["model"]["max_person"],
                                                 cuda=True,
                                                 sigma=1*FACTOR)

        self.test_pose_mgr = models.PoseManager(self.hparams["test_batch"],
                                                self.num_parts,
                                                (INP_RES, INP_RES),
                                                self.hparams["model"]["max_person"],
                                                cuda=True,
                                                sigma=1*FACTOR)
        
        self.train_collate_fn = datasets.COCOPose.collate_function
        self.test_collate_fn = datasets.COCOPose.collate_function

    def epoch(self, epoch):
        self.hparams["learning_rate"] = adjust_learning_rate(
            self.optimizer,
            epoch,
            self.hparams["learning_rate"],
            self.hparams["schedule"],
            self.hparams["lr_gamma"])
        # # decay sigma
        # label_sigma_decay = self.hparams["dataset"]["label_sigma_decay"]
        # if label_sigma_decay > 0:
        #     self.train_dataset.label_sigma *= label_sigma_decay
        #     self.val_dataset.label_sigma *= label_sigma_decayn
    def summary_histogram(self, n_iter):
        for name, param in self.model.named_parameters():
            config.tb_writer.add_histogram("stretch." + name, param.clone().cpu().data.numpy(), n_iter, bins="doane")

    @profile
    def process(self, batch, train, detail=None):
        locate_index = self.hparams["model"]["part_locate"]
        num_regress_stack = self.hparams["model"]["reg_stack"]
        reg_init_noise = self.hparams["model"]["reg_init_noise"]
        reg_mid_noise = self.hparams["model"]["reg_mid_noise"]

        model_pre = self.model[0]
        model_reg = self.model[1]
        criterion_dis = self.criterion

        img, _, mask, extra = batch
        volatile = not train

        img_var = torch.autograd.Variable(img.cuda(async=True), volatile=volatile)
        mask_var = torch.autograd.Variable(mask.cuda(async=True), volatile=volatile)
        keypoint_gt = extra["keypoints_tf_inp"]
        num_gt_per_part = []
        for bi in range(len(keypoint_gt)):
            kpgt_i = keypoint_gt[bi]
            outsider_ids = torch.nonzero(((kpgt_i < 0) | (kpgt_i >= INP_RES))[:, :, :2].sum(dim=-1))
            if len(outsider_ids) > 0:
                keypoint_gt[bi][outsider_ids[:, 0],
                                outsider_ids[:, 1],
                                torch.LongTensor([2]).expand(outsider_ids.size(0))] = 0
            num_gt_per_part.append((kpgt_i[:, :, 2] > 0).long().sum(dim=0))
        num_gt_per_part = torch.stack(num_gt_per_part, dim=0).sum(dim=0)

        locate_gt_idx = [torch.nonzero((kpgt[:, locate_index, 2] > 0))
                            for kpgt in keypoint_gt]
        keypoint_gt = [kpgt[lgti[:, 0]] if len(lgti) > 0 else torch.FloatTensor(0)
                       for kpgt, lgti in zip(keypoint_gt, locate_gt_idx)]
        locate_gt = [kpgt[:, locate_index, :2].long() if len(kpgt) > 0 else torch.LongTensor(0)
                     for kpgt in keypoint_gt]
        locate_init = locate_gt

        reg_init_noise_scale = INP_RES * float(reg_init_noise)
        if reg_init_noise_scale > 0:
            for ib in range(len(locate_init)):
                num_person = len(locate_init[ib])
                if num_person > 0:
                    locate_init_noise = (torch.rand(num_person, 2) * 2 - 1) * reg_init_noise_scale
                    new_locate = locate_init[ib].float() + locate_init_noise
                    new_locate.round_().clamp_(0, INP_RES-1)
                    locate_init[ib] = new_locate.long()

        if np.sum([len(loc_samp) for loc_samp in locate_init]) > 0:
            pose_mgr = self.train_pose_mgr if train else self.test_pose_mgr
            pose_mgr.init_with_locate(locate_init)

            merge_img = model_pre(img_var)
            if detail["summary"]:
                posemap_path = list()
            for i in range(num_regress_stack):
                posemap = pose_mgr.generate()
                if detail["summary"]:
                    posemap_path.append(posemap)
                posemap_var = torch.autograd.Variable(posemap, volatile=volatile)
                move_field, _ = model_reg(posemap_var, merge_img)
                pose_mgr.move_keypoints(move_field, factor=FACTOR)
                # TODO: IMPROVEMENT add noise perturb to keypoints to make model robust
            if detail["summary"]:
                posemap_path.append(posemap)
            # keypoint_gt_cat: #all_match_person x #part x 3
            keypoint_gt_cat = torch.cat(keypoint_gt, dim=0)
            keypoint_gt_cat_mask = torch.autograd.Variable((keypoint_gt_cat[:, :, 2] >= 1).cuda(), requires_grad=False)
            keypoint_gt_cat_var = torch.autograd.Variable(keypoint_gt_cat[:, :, :2].cuda(), requires_grad=False)

            keypoint_pred_cat_var = pose_mgr.all_keypoints_var
            loss = criterion_dis(keypoint_pred_cat_var, keypoint_gt_cat_var, keypoint_gt_cat_mask)

            split = pose_mgr.all_split
            keypoint_pred_cat = keypoint_pred_cat_var.data.cpu()
            keypoint_pred_cat_info = pose_mgr.all_keypoints_info.cpu()
            keypoint_pred = [torch.cat((keypoint_pred_cat[start:end],
                                        keypoint_pred_cat_info[start:end]
                                        .contiguous().float().view(end-start, self.num_parts, 1)),
                                       dim=2) if end > start else torch.FloatTensor(0)
                             for start, end in zip([0] + split[:-1], split)]

            precision, recall, _, _ = PR_multi(keypoint_pred, keypoint_gt, norm=float(OUT_RES)/10, num_parts=self.num_parts)
        else:
            loss = None
            precision = None
            recall = None
            keypoint_pred = [torch.FloatTensor(0) for i in range(len(img))]

        result= {"loss": loss,
                 "acc": recall,
                 "recall": recall,
                 "prec": precision,
                 "index": extra["index"],
                 "pred": keypoint_pred}

        return result