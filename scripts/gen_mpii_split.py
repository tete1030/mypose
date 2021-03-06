import numpy as np
from scipy.io import loadmat
import torch
import sys
import os
import json
import argparse

argparser = argparse.ArgumentParser()
argparser.add_argument("--only-single", action="store_true")
argparser.add_argument("mpii_path")
argparser.add_argument("tompson_path")
argparser.add_argument("generate_file")
args = argparser.parse_args()

mpii_data = loadmat(os.path.join(args.mpii_path, "mpii_human_pose_v1_u12_2/mpii_human_pose_v1_u12_1.mat"))
Tompson_data = loadmat(os.path.join(args.tompson_path, "detections.mat"))
Tompson_img_index = Tompson_data["RELEASE_img_index"]
Tompson_person_index = Tompson_data["RELEASE_person_index"]

Tompson_i_p = np.r_[Tompson_img_index, Tompson_person_index]

train_list = list()
valid_list = list()
neither_list = list()

if args.only_single:
    non_single_valid_list = list()

annolist = mpii_data["RELEASE"][0,0]["annolist"][0]
for i in range(annolist.shape[0]):
    annorect = annolist[i]["annorect"]
    if len(annorect.flat) < 1:
        continue

    if args.only_single:
        single_person_list = mpii_data["RELEASE"][0,0]["single_person"][i,0]
        assert isinstance(single_person_list, np.unsignedinteger) or (type(single_person_list) is np.ndarray)
        if isinstance(single_person_list, np.unsignedinteger):
            single_person_list = [int(single_person_list)]
        else:
            if single_person_list.size > 0:
                assert single_person_list.shape[1] == 1 and single_person_list.dtype == np.uint8
                single_person_list = list(single_person_list[:,0].astype(np.int))
            else:
                single_person_list = []

    for p in range(annorect[0].shape[0]):
        tomp = np.where((Tompson_i_p.T == (i+1, p+1)).all(axis=1))[0]
        person = annorect[0, p]
        try:
            person["annopoints"][0,0]["point"]
            isusable = True
        except (TypeError,ValueError,IndexError):
            isusable = False

        if isusable:
            if tomp.shape[0] > 0:
                if args.only_single and p not in single_person_list:
                    non_single_valid_list.append((i,p))
                valid_list.append((i,p))
            elif (i+1) not in Tompson_img_index[0]:
                if not args.only_single or p in single_person_list:
                    train_list.append((i,p))
                else:
                    neither_list.append((i,p))
        else:
            neither_list.append((i,p))

print(len(train_list))
print(len(valid_list))
print(len(neither_list))
if args.only_single:
    print(len(non_single_valid_list))
torch.save({"train": train_list, "valid": valid_list}, args.generate_file)
