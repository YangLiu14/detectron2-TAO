#!/usr/bin/env python3

import cv2
import glob
import json
import math
import matplotlib.pyplot as plt
import numpy as np
import matplotlib
import argparse
import datetime
import os
import tqdm
import re
import time

from pycocotools.mask import toBbox, encode, decode, area
from collections import Counter
from sklearn import metrics
from typing import List, Dict
from eval.eval_utils import image_stitching

# =======================================================
# Global variables
# =======================================================
"""
known_tao_ids: set of tao ids that can be mapped exactly to coco ids.
neighbor_classes: tao classes that are similar to coco_classes.
unknown_tao_ids: all_tao_ids that exclude known_tao_ids and neighbor_classes..
"""

IOU_THRESHOLD = 0.5

all_ids = set([i for i in range(1, 1231)])

# Category IDs in TAO that are known (appeared in COCO)
with open("../datasets/coco_id2tao_id.json") as f:
    coco_id2tao_id = json.load(f)
known_tao_ids = set([v for k, v in coco_id2tao_id.items()])
# Category IDs in TAO that are unknown (comparing to COCO)
unknown_tao_ids = all_ids.difference(known_tao_ids)

# neighbor classes
with open("../datasets/neighbor_classes.json") as f:
    coco2neighbor_classes = json.load(f)
# Gather tao_ids that can be categorized in the neighbor_classes
neighbor_classes = set()
for coco_id, neighbor_ids in coco2neighbor_classes.items():
    neighbor_classes = neighbor_classes.union(set(neighbor_ids))

# Exclude neighbor classes from unknown_tao_ids
unknown_tao_ids = unknown_tao_ids.difference(neighbor_classes)

# =======================================================
# =======================================================


def score_func(prop):
    if FLAGS.score_func == "score":
        return prop["score"]
    if FLAGS.score_func == "bgScore":
        return prop["bg_score"]
    if FLAGS.score_func == "1-bgScore":
        if FLAGS.postNMS:
            return prop['one_minus_bg_score']
        else:
            return 1 - prop["bg_score"]
    if FLAGS.score_func == "objectness":
        return prop["objectness"]
    if FLAGS.score_func == "bg+objectness":
        if FLAGS.postNMS:
            return prop['bg_rpn_sum']
        else:
            return (1000 * prop["objectness"] + prop["bg_score"]) / 2
    if FLAGS.score_func == "bg*objectness":
        if FLAGS.postNMS:
            return prop['bg_rpn_product']
        else:
            return math.sqrt(1000 * prop["objectness"] * prop["bg_score"])


# ==========================================================================================
# Loading functions for GT and Proposals
# ==========================================================================================
def load_gt(exclude_classes=(), ignored_sequences=(), prefix_dir_name='oxford_labels',
            dist_thresh=1000.0, area_thresh=10 * 10):
    with open(prefix_dir_name, 'r') as f:
        gt_json = json.load(f)

    # gt = { 'sequence_name': [list of bboxes(tuple)] }
    # n_boxes

    videos = gt_json['videos']
    annotations = gt_json['annotations']
    tracks = gt_json['tracks']
    images = gt_json['images']
    info = gt_json['info']
    categories = gt_json['categories']

    gt = {}
    imgID2fname = dict()
    for img in images:
        imgID2fname[img['id']] = img['file_name']

    nbox_ArgoVerse, nbox_BDD, nbox_Charades, nbox_LaSOT, nbox_YFCC100M, nbox_AVA, nbox_HACS = 0, 0, 0, 0, 0, 0, 0

    for ann in annotations:
        if ann["category_id"] in exclude_classes:
            continue

        img_id = ann['image_id']
        fname = imgID2fname[img_id]
        fname = fname.replace("jpg", "json")

        # ignore certain data souces
        src_name = fname.split("/")[1]
        if src_name in ignored_sequences:
            continue
        # Count n_gt_boxes in each sub-dataset
        if src_name == 'ArgoVerse':
            nbox_ArgoVerse += 1
        elif src_name == 'BDD':
            nbox_BDD += 1
        elif src_name == 'Charades':
            nbox_Charades += 1
        elif src_name == 'LaSOT':
            nbox_LaSOT += 1
        elif src_name == 'YFCC100M':
            nbox_YFCC100M += 1
        elif src_name == 'AVA':
            nbox_AVA += 1
        elif src_name == 'HACS':
            nbox_HACS += 1

        xc, yc, w, h = ann['bbox']
        # convert [xc, yc, w, h] to [x1, y1, x2, y2]
        box = (xc, yc, w, h)
        bbox = [box[0], box[1], box[0] + box[2], box[1] + box[3]]
        if fname in gt.keys():
            gt[fname]['bboxes'].append(bbox)
            gt[fname]['track_ids'].append(ann['track_id'])
        else:
            gt[fname] = {'bboxes': [], 'track_ids': []}
            gt[fname]['bboxes'].append(bbox)
            gt[fname]['track_ids'].append(ann['track_id'])

    n_boxes = sum([len(x['bboxes']) for x in gt.values()], 0)
    assert n_boxes == nbox_ArgoVerse + nbox_BDD + nbox_Charades + nbox_LaSOT + nbox_YFCC100M + nbox_AVA + nbox_HACS
    print("number of gt boxes", n_boxes)
    nbox = {"ArgoVerse": nbox_ArgoVerse,
            "BDD": nbox_BDD,
            "Charades": nbox_Charades,
            "LaSOT": nbox_LaSOT,
            "YFCC100M": nbox_YFCC100M,
            'AVA': nbox_AVA,
            'HACS': nbox_HACS}
    return gt, n_boxes, nbox


def load_gt_categories(exclude_classes=(), ignored_sequences=(), prefix_dir_name='oxford_labels'):
    gt_jsons = glob.glob("%s/*/*.json" % prefix_dir_name)

    gt_cat = {}
    gt_cat_map = {}

    cat_idx = 0
    for gt_json in gt_jsons:

        # Exclude from eval
        matching = [s for s in ignored_sequences if s in gt_json]
        if len(matching) > 0: continue

        anns = json.load(open(gt_json))

        categories = []
        for ann in anns:
            cat_str = ann["category"]
            if cat_str in exclude_classes:
                continue
            categories.append(cat_str)

            if cat_str not in gt_cat_map:
                gt_cat_map[cat_str] = cat_idx
                cat_idx += 1

        gt_cat[gt_json] = categories
    n_boxes = sum([len(x) for x in gt_cat.values()], 0)
    print("number of gt boxes", n_boxes)
    return gt_cat, n_boxes, gt_cat_map


def load_proposals(folder, gt, ignored_sequences=(), score_fnc=score_func):
    proposals = {}
    for filename in tqdm.tqdm(gt.keys()):

        if filename.split('/')[-3] != folder.split('/')[-1]:
            continue
        prop_filename = os.path.join(folder, "/".join(filename.split("/")[-2:]))

        # Exclude from eval
        matching = [s for s in ignored_sequences if s in filename]
        if len(matching) > 0:
            continue

        # Load proposals
        # try:
        #     props = json.load(open(prop_filename))
        # except ValueError:
        #     print("Error loading json: %s" % prop_filename)
        #     quit()
        try:
            # props = json.load(open(prop_filename))
            prop_filename = prop_filename.replace(".json", ".npz")
            props = np.load(prop_filename, allow_pickle=True)['arr_0'].tolist()
        except:
            print(prop_filename, "not found")
            continue

        if props is None:
            continue

        props = sorted(props, key=score_fnc, reverse=True)
        if FLAGS.nonOverlap:
            props = remove_mask_overlap(props)
        if FLAGS.nonOverlap_small:
            props = remove_mask_overlap_small_on_top(props)
        props = sorted(props, key=score_fnc, reverse=True)

        if "bbox" in props[0]:
            bboxes = [prop["bbox"] for prop in props]
        else:
            bboxes = [toBbox(prop["instance_mask"]) for prop in props]
            # The output box from cocoapi.mask.toBbox gives [xc, yc, w, h]
            for i in range(len(bboxes)):
                box = bboxes[i]  # in the form of [xc, yc, w, h]
                # convert [xc, yc, w, h] to [x1, y1, x2, y2]
                bbox = [box[0], box[1], box[0] + box[2], box[1] + box[3]]
                bboxes[i] = bbox

        # convert from [x0, y0, w, h] (?) to [x0, y0, x1, y1]
        # bboxes = [[box[0], box[1], box[0] + box[2], box[1] + box[3]] for box in bboxes]
        proposals[filename] = bboxes

    return proposals


# =========================================================================
# Non Overlap
# =========================================================================
def intersects(box1, box2):
    """
    Check if two boxes intersects with each other.
    Box are in the form of [x1, y1, x2, y2]
    Returns:
        bool
    """
    max_x1 = max(box1[0], box2[0])
    max_y1 = max(box1[1], box2[1])
    min_x2 = min(box1[2], box2[2])
    min_y2 = min(box2[3], box2[3])

    if max_x1 < min_x2 and max_y1 < min_y2:
        return True
    else:
        return False


def remove_mask_overlap(proposals):
    """
    Args:
        proposals: List[Dict], sorted proposals according specific scoring criterion. Each proposal contains:
        {
            category_id: int,
            bbox: [x1, y1, x2, y2],
            score: float (could be named differently, e.g. bg_score, objectness, etc)
            instance_mask: COCO_RLE format,
        }

    Returns:
        selected_props, List[Dict]
    """
    masks = [decode(prop['instance_mask']) for prop in proposals]
    idx = [i for i in range(len(proposals))]
    labels = np.arange(1, len(proposals) + 1)
    png = np.zeros_like(masks[0])

    # Put the mask there in reversed order, so that the latter one would just cover the previous one,
    # and the latter one has higher score. (Because proposals are sorted)
    for i in reversed(range(len(proposals))):
        png[masks[i].astype("bool")] = labels[i]

    refined_masks = [(png == id_).astype(np.uint8) for id_ in labels]
    refined_segmentations = [encode(np.asfortranarray(refined_mask)) for refined_mask in refined_masks]
    selected_props = []
    for prop, refined_segmentation, mask in zip(proposals, refined_segmentations, refined_masks):
        refined_segmentation['counts'] = refined_segmentation['counts'].decode("utf-8")
        if area(refined_segmentation) == 0:
            continue
        prop['instance_mask'] = refined_segmentation
        box = toBbox(refined_segmentation).tolist()  # in the form of [xc, yc, w, h]
        # convert [xc, yc, w, h] to [x1, y1, x2, y2]
        bbox = [box[0], box[1], box[0] + box[2], box[1] + box[3]]
        prop['bbox'] = bbox
        # prop['mask'] = mask

        selected_props.append(prop)

    return selected_props


def remove_mask_overlap_small_on_top(proposals):
    """
    Similar with `remove_mask_overlap`, but instead of putting masks with higher scores on top,
    put masks with smaller area on top.

    Args:
        proposals: List[Dict], sorted proposals according specific scoring criterion. Each proposal contains:
        {
            category_id: int,
            bbox: [x1, y1, x2, y2],
            score: float (could be named differently, e.g. bg_score, objectness, etc)
            instance_mask: COCO_RLE format,
        }

    Returns:
        selected_props, List[Dict]
    """
    # Sort proposals by it's area.
    # all_areas = [area(prop['instance_mask']) for prop in proposals]
    proposals.sort(key=lambda prop: area(prop['instance_mask']))
    # all_areas2 = [area(prop['instance_mask']) for prop in proposals]

    masks = [decode(prop['instance_mask']) for prop in proposals]
    idx = [i for i in range(len(proposals))]
    labels = np.arange(1, len(proposals) + 1)
    png = np.zeros_like(masks[0])

    # Put the mask there in reversed order, so that the latter one would just cover the previous one,
    # and the latter one has smaller scores. (Because proposals are sorted by area)
    for i in reversed(range(len(proposals))):
        png[masks[i].astype("bool")] = labels[i]

    refined_masks = [(png == id_).astype(np.uint8) for id_ in labels]
    refined_segmentations = [encode(np.asfortranarray(refined_mask)) for refined_mask in refined_masks]
    selected_props = []
    for prop, refined_segmentation, mask in zip(proposals, refined_segmentations, refined_masks):
        refined_segmentation['counts'] = refined_segmentation['counts'].decode("utf-8")
        if area(refined_segmentation) == 0:
            continue
        prop['instance_mask'] = refined_segmentation
        box = toBbox(refined_segmentation).tolist()  # in the form of [xc, yc, w, h]
        # convert [xc, yc, w, h] to [x1, y1, x2, y2]
        bbox = [box[0], box[1], box[0] + box[2], box[1] + box[3]]
        prop['bbox'] = bbox
        # prop['mask'] = mask

        selected_props.append(prop)

    return selected_props


def non_overlap_filter(proposals_per_frame: List[Dict]):
    """
    Filter out overlapping bboxes. If two bboxes are overlapping, keep the bbox with the higher score,
    according to the score_func.
    The bbox is already sorted according to score_fnc.

    Returns:
        remaining proposals. Same data structure as the input.
    """
    # Find out the index of the proposals to be filtered out.
    delete_idx = list()
    N = len(proposals_per_frame)
    for i in range(N):
        if i in delete_idx:
            continue
        box1 = proposals_per_frame[i]['bbox']
        for j in range(i + 1, N):
            if j in delete_idx:
                continue
            box2 = proposals_per_frame[j]['bbox']
            if intersects(box1, box2):
                delete_idx.append(j)
    delete_idx.sort()
    # print("box deleted: {} of {}".format(len(delete_idx), N))

    # Get remained proposals
    remain_props = list()
    for i in range(N):
        if delete_idx and i == delete_idx[0]:
            delete_idx.pop(0)
            continue
        else:
            remain_props.append(proposals_per_frame[i])

    return remain_props


def calculate_ious(bboxes1, bboxes2):
    """
    :param bboxes1: Kx4 matrix, assume layout (x0, y0, x1, y1)
    :param bboxes2: Nx$ matrix, assume layout (x0, y0, x1, y1)
    :return: KxN matrix of IoUs
    """
    min_ = np.minimum(bboxes1[:, np.newaxis, :], bboxes2[np.newaxis, :, :])
    max_ = np.maximum(bboxes1[:, np.newaxis, :], bboxes2[np.newaxis, :, :])
    I = np.maximum(min_[..., 2] - max_[..., 0], 0) * np.maximum(min_[..., 3] - max_[..., 1], 0)
    area1 = (bboxes1[..., 2] - bboxes1[..., 0]) * (bboxes1[..., 3] - bboxes1[..., 1])
    area2 = (bboxes2[..., 2] - bboxes2[..., 0]) * (bboxes2[..., 3] - bboxes2[..., 1])
    U = area1[:, np.newaxis] + area2[np.newaxis, :] - I
    assert (U > 0).all()
    IOUs = I / U
    assert (IOUs >= 0).all()
    assert (IOUs <= 1).all()
    return IOUs


def evaluate_proposals(gt, props, n_max_proposals=1000):
    all_ious = []  # ious for all frames
    all_track_ids = []
    for img, img_gt in gt.items():
        if len(img_gt) == 0:
            continue

        try:
            img_props = props[img]
        except:
            continue

        gt_bboxes = np.array(img_gt['bboxes'])
        track_ids = np.array(img_gt['track_ids'])
        prop_bboxes = np.array(img_props)
        ious = calculate_ious(gt_bboxes, prop_bboxes)

        # pad to n_max_proposals
        ious_padded = np.zeros((ious.shape[0], n_max_proposals))
        ious_padded[:, :ious.shape[1]] = ious[:, :n_max_proposals]
        all_ious.append(ious_padded)
        all_track_ids.append(track_ids)
    all_ious = np.concatenate(all_ious)
    all_track_ids = np.concatenate(all_track_ids)
    # Calculate the track-length of each track id
    gt_track_len = Counter()
    for t_id in all_track_ids:
        gt_track_len[t_id] += 1

    if IOU_THRESHOLD is None:
        iou_curve = [0.0 if n_max == 0 else all_ious[:, :n_max].max(axis=1).mean() for n_max in
                     range(0, n_max_proposals + 1)]
    else:
        assert 0 <= IOU_THRESHOLD <= 1
        if FLAGS.recall_based_on == "gt_bboxes":
            iou_curve = [0.0 if n_max == 0 else (all_ious[:, :n_max].max(axis=1) > IOU_THRESHOLD).mean() for n_max in
                         range(0, n_max_proposals + 1)]
        elif FLAGS.recall_based_on == "tracks":
            iou_curve = list()
            for n_max in range(0, n_max_proposals + 1):
                if n_max == 0:
                    iou_curve.append(0.0)
                else:
                    mask = (all_ious[:, :n_max].max(axis=1) > IOU_THRESHOLD)
                    recalled_track_ids = all_track_ids[mask]
                    ratio = len(set(recalled_track_ids)) / len(set(all_track_ids))
                    iou_curve.append(ratio)
        else:
            raise Exception(FLAGS.recall_based_on, "is not a valid option, choose from [gt_bboxes, tracks]")
        # elif FLAGS.recall_based_on == "tracks":
        #     iou_curve = list()
        #     # N here means: we only consider a track is successfully recalled,
        #     # when there are at least % of objects in this track are detected.
        #     for N in range(1, 101):
        #         mask = (all_ious[:, :1000].max(axis=1) > IOU_THRESHOLD)
        #         recalled_track_ids = all_track_ids[mask]
        #
        #         # Count how many times one track_id is being recalled
        #         recalled_track_ids = recalled_track_ids.tolist()
        #         track_id2cnt = Counter()
        #         for t_id in recalled_track_ids:
        #             track_id2cnt[t_id] += 1
        #
        #         recalled_track_ids = list()
        #         for t_id, cnt in track_id2cnt.items():
        #             gt_len = gt_track_len[t_id]  # length of the gt_track
        #             assert cnt <= gt_len
        #             if cnt / gt_len * 100 >= N:
        #                 recalled_track_ids.append(t_id)
        #
        #         ratio = len(set(recalled_track_ids)) / len(set(all_track_ids))
        #         iou_curve.append(ratio)
        # else:
        #     raise Exception(FLAGS.recall_based_on, "is not a valid option, choose from [gt_bboxes, tracks]")

    return iou_curve


def evaluate_folder(gt, folder, ignored_sequences=(), score_fnc=score_func):
    print("Loading proposals from", folder)
    props = load_proposals(folder, gt, ignored_sequences=ignored_sequences, score_fnc=score_fnc)

    # iou_curve = evaluate_proposals(gt, props, n_max_proposals=200)
    iou_curve = evaluate_proposals(gt, props, n_max_proposals=1000)

    iou_50 = iou_curve[50]
    # iou_100 = iou_curve[100]
    # iou_150 = iou_curve[150]
    # iou_200 = iou_curve[200]
    # iou_700 = iou_curve[700]
    # iou_900 = iou_curve[900]
    # iou_1k = iou_curve[1000]

    end_iou = iou_curve[-1]

    method_name = os.path.basename(os.path.dirname(folder + "/"))

    # print("%s: R50: %1.2f, R100: %1.2f, R150: %1.2f, R200: %1.2f, R_total: %1.2f" %
    #       (method_name,
    #        iou_50,
    #        iou_100,
    #        iou_150,
    #        iou_200,
    #        # iou_700,
    #        end_iou))

    return iou_curve


def title_to_filename(plot_title):
    filtered_title = re.sub("[\(\[].*?[\)\]]", "", plot_title)  # Remove the content within the brackets
    filtered_title = filtered_title.replace("_", "").replace(" ", "").replace(",", "_")
    return filtered_title


def make_plot(export_dict, plot_title, x_vals, linewidth=5):
    plt.figure()

    itm = export_dict.items()
    itm = sorted(itm, reverse=True)
    for idx, item in enumerate(itm):
        # Compute Area Under Curve
        x = x_vals[0:200]
        y = item[1]['data'][0:200]
        auc = round(metrics.auc(x, y), 2)
        # curve_label = item[0].replace('.', '')
        curve_label = item[0].replace('.', '') + ': nbox(gt)=' + str(item[1]['nbox_gt']) + ', AUC=' + str(auc)
        # plt.plot(x_vals[0:700], item[1][0:700], label=curve_label, linewidth=linewidth)
        plt.plot(x_vals[0:200], item[1]['data'][0:200], label=curve_label, linewidth=linewidth)

    ax = plt.gca()
    ax.set_yticks(np.arange(0, 1.2, 0.2))
    # ax.set_xticks(np.asarray([25, 100, 200, 300, 500, 700, 900, 1000]))
    ax.set_xticks(np.asarray([25, 50, 75, 100, 125, 150, 175, 200]))
    plt.xlabel("$\#$ proposals {}".format(FLAGS.score_func))
    plt.ylabel("Recall")
    ax.set_ylim([0.0, 1.0])
    plt.legend(prop={"size": 8})
    plt.grid()
    plt.title(plot_title)


def make_plot_combined(export_dict, plot_title, x_vals, linewidth=5, npoints=200):
    plt.figure()

    itm = export_dict.items()
    itm = sorted(itm, reverse=True)
    weighted_y = np.zeros((npoints,))
    nbox_sum = 0
    for idx, item in enumerate(itm):
        nbox_sum += item[1]['nbox_gt']

    for idx, item in enumerate(itm):
        y = np.array(item[1]['data'][0:npoints])
        # y = np.array(item[1]['data'][0:1000])
        weighted_y += (item[1]['nbox_gt'] / nbox_sum) * y

    x = x_vals[0:npoints]
    # x = x_vals[0:1000]
    y = weighted_y.tolist()
    auc = round(metrics.auc(x, y), 2)
    curve_label = 'nbox(gt)=' + str(nbox_sum) + ', AUC=' + str(auc)
    # plt.plot(x_vals[0:200], y, label=curve_label, linewidth=linewidth)
    plt.plot(x_vals[0:npoints], y, label=curve_label, linewidth=linewidth)

    ax = plt.gca()
    ax.set_yticks(np.arange(0, 1.2, 0.2))
    if npoints == 200:
        ax.set_xticks(np.asarray([25, 50, 75, 100, 125, 150, 175, 200]))
    elif npoints == 1000:
        ax.set_xticks(np.asarray([25, 100, 200, 300, 500, 700, 900, 1000]))
    elif npoints == 100:
        ax.set_xticks(np.asarray([5, 10, 20, 30, 50, 70, 90, 100]))

    plt.xlabel("$\#$ proposals {}".format(FLAGS.score_func))
    plt.ylabel("Recall")
    ax.set_ylim([0.0, 1.0])
    plt.legend(prop={"size": 8})
    plt.grid()
    plt.title(plot_title)

    return y, auc


def export_figs(scoring, export_dict, plot_title, output_dir, x_vals):
    if not os.path.exists(output_dir + '/csv'):
        os.makedirs(output_dir + '/csv')

    # Export figs, csv
    if output_dir is not None:
        plt.savefig(os.path.join(output_dir, title_to_filename(plot_title) + "_" + FLAGS.score_func + ".png"),
                    bbox_inches='tight')

        # Save to csv
        np.savetxt(os.path.join(output_dir, 'num_objects.csv'), np.array(x_vals), delimiter=',', fmt='%d')
        for item in export_dict.items():
            split = plot_title.split()[0]
            np.savetxt(os.path.join(output_dir, 'csv', item[0] + '_' + scoring + '_' + split + '.csv'), item[1]['data'], delimiter=',', fmt='%1.4f')

def export_csvs(scoring, export_dict, plot_title, output_dir, x_vals):
    if not os.path.exists(output_dir + '/csv'):
        os.makedirs(output_dir + '/csv')

    # Export figs, csv
    if output_dir is not None:
        plt.savefig(os.path.join(output_dir, title_to_filename(plot_title) + "_" + FLAGS.score_func + ".png"),
                    bbox_inches='tight')

        # Save to csv
        np.savetxt(os.path.join(output_dir, 'num_objects.csv'), np.array(x_vals), delimiter=',', fmt='%d')
        for item in export_dict.items():
            split = plot_title.split()[0]
            np.savetxt(os.path.join(output_dir, 'csv', item[0] + '_' + scoring + '_' + split + '.csv'), item[1]['data'], delimiter=',', fmt='%1.4f')


def evaluate_all_folders_oxford(scoring, gt, plot_title, n_subset_gt_boxes, user_specified_result_dir=None, output_dir=None):
    print("----------- Evaluate Oxford Recall -----------")

    # Export dict
    export_dict = {

    }

    # +++ User-specified +++
    user_specified_results = None
    if user_specified_result_dir is not None:
        dirs = os.listdir(user_specified_result_dir)
        dirs.sort()

        # ignore_dirs = ["BDD", "Charades", "LaSOT", "YFCC100M", "HACS", "AVA"]
        ignore_dirs = []
        for mydir in dirs:
            if mydir[0] == '.':
                continue  # Filter out `.DS_Store` and `._.DS_Store`
            if mydir in ignore_dirs:
                continue

            print("---Eval: %s ---" % mydir)
            user_specified_results = evaluate_folder(gt, os.path.join(user_specified_result_dir, mydir))
            # export_dict[mydir] = user_specified_results
            export_dict[mydir] = dict()
            export_dict[mydir]['data'] = user_specified_results
            export_dict[mydir]['nbox_gt'] = n_subset_gt_boxes[mydir]

    x_vals = range(1001)

    # Plot everything specified via export_dict
    # make_plot(export_dict, plot_title, x_vals)
    y, auc = make_plot_combined(export_dict, plot_title, x_vals, npoints=100)
    # y, auc = calc_combined_recall_curve(export_dict, plot_title, x_vals)

    # Export figs, csv
    # export_figs(scoring, export_dict, plot_title, output_dir, x_vals)
    export_csvs(scoring, export_dict, plot_title, output_dir, x_vals)

    split = plot_title.split()[0]
    np.savetxt(os.path.join(output_dir, split + '_' + scoring + '.csv'), np.array(y), delimiter=',', fmt='%1.4f')


def eval_recall_oxford(output_dir, scoring, split):
    # ignored_seq = ("BDD", "Charades", "LaSOT", "YFCC100M", "HACS", "AVA")
    ignored_seq = ()


    if split == "unknown":
        # +++ "unknown" categories +++
        print("evaluating unknown:", scoring)
        exclude_classes = tuple(known_tao_ids.union(neighbor_classes))
        gt, n_gt_boxes, n_subset_gt_boxes = load_gt(exclude_classes, ignored_seq, prefix_dir_name=FLAGS.labels)

        evaluate_all_folders_oxford(scoring, gt, "unknown classes (" + str(n_gt_boxes) + " bounding boxes)",
                                    n_subset_gt_boxes=n_subset_gt_boxes,
                                    output_dir=output_dir,
                                    user_specified_result_dir=FLAGS.evaluate_dir)
    elif split == "known":
        # +++ "known" categories +++
        print("evaluating coco 78 classes:", scoring)
        exclude_classes = tuple(unknown_tao_ids.union(neighbor_classes))
        gt, n_gt_boxes, n_subset_gt_boxes = load_gt(exclude_classes, ignored_seq, prefix_dir_name=FLAGS.labels)
        # gt, n_gt_boxes = load_gt_oxford(exclude_classes, prefix_dir_name=FLAGS.labels)

        evaluate_all_folders_oxford(scoring, gt, "known classes (" + str(n_gt_boxes) + " bounding boxes)",
                                    n_subset_gt_boxes=n_subset_gt_boxes,
                                    output_dir=output_dir,
                                    user_specified_result_dir=FLAGS.evaluate_dir)

    elif split == "neighbor":
        # +++ "neighbor" categories +++
        print("evaluating neighbor classes:", scoring)
        exclude_classes = tuple(known_tao_ids.union(unknown_tao_ids))
        gt, n_gt_boxes, n_subset_gt_boxes = load_gt(exclude_classes, ignored_seq, prefix_dir_name=FLAGS.labels)

        evaluate_all_folders_oxford(scoring, gt, "neighbor classes (" + str(n_gt_boxes) + " bounding boxes)",
                                    n_subset_gt_boxes=n_subset_gt_boxes,
                                    output_dir=output_dir,
                                    user_specified_result_dir=FLAGS.evaluate_dir)



def main(score_func):
    # Matplotlib params
    matplotlib.rcParams.update({'font.size': 15})
    matplotlib.rcParams.update({'font.family': 'sans-serif'})
    matplotlib.rcParams['text.usetex'] = True

    # Prep output dir (if specified)
    output_dir = None
    if FLAGS.plot_output_dir is not None:
        timestamp = datetime.datetime.fromtimestamp(time.time()).strftime('%Y_%m_%d__%H_%M_%S')

        if FLAGS.do_not_timestamp:
            timestamp = ""

        output_dir = os.path.join(FLAGS.plot_output_dir, timestamp)
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        eval_recall_oxford(output_dir=output_dir, scoring=score_func, split=FLAGS.split)


# def modified_main(split: str):
#     ignored_seq = ()
#     if split == "unknown":
#         # +++ "unknown" categories +++
#         print("evaluating unknown:")
#         exclude_classes = tuple(known_tao_ids.union(neighbor_classes))
#         gt, n_gt_boxes, n_subset_gt_boxes = load_gt(exclude_classes, ignored_seq, prefix_dir_name=FLAGS.labels)
#
#         props_dir = FLAGS.evaluate_dir
#         for video_folder in props_dir:
#         print("Loading proposals from", video_folder)
#         props = load_proposals(video_folder, gt, ignored_sequences=[], score_fnc=score_fnc)
#
#
#         evaluate_all_folders_oxford(scoring, gt, "unknown classes (" + str(n_gt_boxes) + " bounding boxes)",
#                                     n_subset_gt_boxes=n_subset_gt_boxes,
#                                     output_dir=output_dir,
#                                     user_specified_result_dir=FLAGS.evaluate_dir)



if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    # Args
    parser.add_argument('--plot_output_dir', type=str, help='Plots output dir.')
    parser.add_argument('--props_base_dir', type=str, help='Base directory of the proposals to be evaluated.')
    parser.add_argument('--evaluate_dir', type=str, help='Dir containing result files that you want to evaluate')
    parser.add_argument('--labels', type=str, default='',
                        help='Specify dir containing the labels')
    # parser.add_argument('--labels', type=str, default='oxford_labels',
    #                     help='Specify dir containing the labels')
    parser.add_argument('--recall_based_on', required=True, type=str, help='Calculate recall based on' +
                                                                           'number of GT-bbox or on number of tracks')
    parser.add_argument('--score_func', type=str, help='Sorting criterion to use. Choose from' +
                                                       '[score, bg_score, 1-bg_score, rpn, bg+rpn, bg*rpn]')
    parser.add_argument('--postNMS', action='store_true', help='processing postNMS proposals.')
    parser.add_argument('--nonOverlap', action='store_true', help='Filter out the overlapping bboxes in proposals')
    parser.add_argument('--nonOverlap_small', action='store_true', help='Filter out the overlapping bboxes in proposals')
    parser.add_argument('--do_not_timestamp', action='store_true', help='Dont timestamp output dirs')
    parser.add_argument('--split', type=str)

    FLAGS = parser.parse_args()

    # Check args
    if FLAGS.nonOverlap:
        print(">>> non-overlap (higher confidence mask on top)")
    if FLAGS.nonOverlap_small:
        print(">>> non-overlap (smaller mask on top")
    if FLAGS.recall_based_on not in ['gt_bboxes', 'tracks']:
        raise Exception(FLAGS.recall_based_on, "is not a valid option, choose from [gt_bboxes, tracks]")

    if FLAGS.postNMS:
        props_dirs = ["_bg_score",
                      "_bg_rpn_product",
                      "_bg_rpn_sum",
                      "_objectness",
                      "_one_minus_bg_score",
                      "_score"]
    else:
        props_dirs = [""]

    props_dirs = [FLAGS.props_base_dir + p for p in props_dirs]
    score_funcs = ["bgScore", "bg*objectness", "bg+objectness", "objectness", "1-bgScore", "score"]

    if FLAGS.postNMS:
        for eval_dir, score_f in zip(props_dirs, score_funcs):
            print("(postNMS) Processing", eval_dir, "using", score_f)
            FLAGS.evaluate_dir = eval_dir
            FLAGS.score_func = score_f
            main(FLAGS.score_func)
    else:
        for score_f in score_funcs:
            print("(normal NMS) Processing", props_dirs[0], "using", score_f)
            FLAGS.evaluate_dir = props_dirs[0]
            FLAGS.score_func = score_f
            main(FLAGS.score_func)

    # Combine the images
    image_paths = ["COCOunknownclasses_score.png", "COCOunknownclasses_bgScore.png", "COCOunknownclasses_1-bgScore.png",
                   "COCOunknownclasses_objectness.png", "COCOunknownclasses_bg+rpn.png",
                   "COCOunknownclasses_bg*rpn.png",
                   "COCOneighborclasses_score.png", "COCOneighborclasses_bgScore.png",
                   "COCOneighborclasses_1-bgScore.png", "COCOneighborclasses_objectness.png",
                   "COCOneighborclasses_bg+rpn.png", "COCOneighborclasses_bg*rpn.png",
                   "COCOknownclasses_score.png", "COCOknownclasses_bgScore.png", "COCOknownclasses_1-bgScore.png",
                   "COCOknownclasses_objectness.png", "COCOknownclasses_bg+rpn.png", "COCOknownclasses_bg*rpn.png"]
    # image_paths = ["COCOunknownclasses_score.png", "COCOunknownclasses_bgScore.png", "COCOunknownclasses_1-bgScore.png",
    #                "COCOunknownclasses_objectness.png", "COCOunknownclasses_bg+rpn.png",
    #                "COCOunknownclasses_bg*rpn.png",
    #                "COCOneighborclasses_score.png", "COCOneighborclasses_bgScore.png",
    #                "COCOneighborclasses_1-bgScore.png", "COCOneighborclasses_objectness.png",
    #                "COCOneighborclasses_bg+rpn.png", "COCOneighborclasses_bg*rpn.png"]
    root_dir = FLAGS.plot_output_dir
    image_paths = [root_dir + i for i in image_paths]

    output_path = FLAGS.plot_output_dir + "combined.png"
    # image_stitching(image_paths, 6, 3, output_path)
    # image_stitching(image_paths, 6, 2, output_path)

    # Delete the images
    # print("Deleting images")
    # for ip in image_paths:
    #     os.remove(ip)
