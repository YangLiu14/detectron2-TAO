#!/usr/bin/env python3

import glob
import json
import matplotlib.pyplot as plt
import numpy as np
from pycocotools.mask import toBbox
import matplotlib
import argparse
import datetime
import time
import re
import os

IOU_THRESHOLD = 0.5

all_ids = set([i for i in range(1, 1231)])

# Category IDs in TAO that are known (appeared in COCO)
with open("../datasets/coco_id2tao_id.json") as f:
    coco_id2tao_id = json.load(f)
known_tao_ids = set([v for k, v in coco_id2tao_id.items()])

# Category IDs in TAO that are unknown (comparing to COCO)
unknown_tao_ids = all_ids.difference(known_tao_ids)


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

    cat_id2tracks = dict()
    for track in tracks:
        track_id = track['id']
        cat_id = track['category_id']
        if cat_id not in cat_id2tracks.keys():
            cat_id2tracks[cat_id] = [track_id]
        else:
            cat_id2tracks[cat_id].append(track_id)

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

        xc, yc, w, h = ann['bbox']
        # convert [xc, yc, w, h] to [x1, y1, x2, y2]
        box = (xc, yc, w, h)
        bbox = [box[0], box[1], box[0] + box[2], box[1] + box[3]]
        if fname in gt.keys():
            gt[fname].append({"bbox": bbox,
                              "cat_id": ann['category_id'],
                              "track_id": ann['category_id']
                              })
        else:
            gt[fname] = [{"bbox": bbox,
                          "cat_id": ann['category_id'],
                          "track_id": ann['category_id']}]

    n_boxes = sum([len(x) for x in gt.values()], 0)
    print("number of gt boxes", n_boxes)
    return gt, n_boxes, cat_id2tracks


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


# def load_proposals(folder, gt, ignored_sequences=(), score_fnc=lambda prop: prop["score"]):
def load_proposals(folder, gt, ignored_sequences=(), score_fnc=lambda prop: 1 - prop["bg_score"]):
    proposals = {}
    for filename in gt.keys():
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
            props = json.load(open(prop_filename))
        except:
            print(prop_filename, "not found")
            continue

        if props is None:
            continue

        props = sorted(props, key=score_fnc, reverse=True)

        if "bbox" in props[0]:
            bboxes = [prop["bbox"] for prop in props]
        else:
            bboxes = [toBbox(prop["segmentation"]) for prop in props]

        # convert from [x0, y0, w, h] (?) to [x0, y0, x1, y1]
        # bboxes = [[box[0], box[1], box[0] + box[2], box[1] + box[3]] for box in bboxes]
        proposals[filename] = bboxes

    return proposals


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
    for img, img_gt in gt.items():
        if len(img_gt) == 0:
            continue

        try:
            img_props = props[img]
        except:
            continue

        gt_bboxes = np.array([box for box in img_gt['bbox']])
        import pdb; pdb.set_trace()
        prop_bboxes = np.array(img_props)
        ious = calculate_ious(gt_bboxes, prop_bboxes)

        # pad to n_max_proposals
        ious_padded = np.zeros((ious.shape[0], n_max_proposals))
        ious_padded[:, :ious.shape[1]] = ious[:, :n_max_proposals]
        all_ious.append(ious_padded)
    all_ious = np.concatenate(all_ious)

    if IOU_THRESHOLD is None:
        iou_curve = [0.0 if n_max == 0 else all_ious[:, :n_max].max(axis=1).mean() for n_max in
                     range(0, n_max_proposals + 1)]
    else:
        assert 0 <= IOU_THRESHOLD <= 1
        iou_curve = [0.0 if n_max == 0 else (all_ious[:, :n_max].max(axis=1) > IOU_THRESHOLD).mean() for n_max in
                     range(0, n_max_proposals + 1)]
    return iou_curve


# def evaluate_folder(gt, folder, ignored_sequences=(), score_fnc=lambda prop: prop["score"]):
def evaluate_folder(gt, folder, ignored_sequences=(), score_fnc=lambda prop: 1 - prop["bg_score"]):
    props = load_proposals(folder, gt, ignored_sequences=ignored_sequences, score_fnc=score_fnc)

    iou_curve = evaluate_proposals(gt, props)

    iou_50 = iou_curve[50]
    iou_100 = iou_curve[100]
    iou_150 = iou_curve[150]
    iou_200 = iou_curve[200]
    iou_700 = iou_curve[700]
    end_iou = iou_curve[-1]

    method_name = os.path.basename(os.path.dirname(folder + "/"))

    print("%s: R50: %1.2f, R100: %1.2f, R150: %1.2f, R200: %1.2f, R700: %1.2f, R_total: %1.2f" %
          (method_name,
           iou_50,
           iou_100,
           iou_150,
           iou_200,
           iou_700,
           end_iou))

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
        curve_label = item[0].replace('.', '')
        plt.plot(x_vals[0:700], item[1][0:700], label=curve_label, linewidth=linewidth)

    ax = plt.gca()
    ax.set_yticks(np.arange(0, 1.2, 0.2))
    ax.set_xticks(np.asarray([25, 100, 200, 300, 500, 700]))
    plt.xlabel("$\#$ proposals")
    plt.ylabel("Recall")
    ax.set_ylim([0.0, 1.0])
    plt.legend()
    plt.grid()
    plt.title(plot_title)


def export_figs(export_dict, plot_title, output_dir, x_vals):
    # Export figs, csv
    if output_dir is not None:
        plt.savefig(os.path.join(output_dir, title_to_filename(plot_title) + ".pdf"), bbox_inches='tight')

        # Save to csv
        np.savetxt(os.path.join(output_dir, 'num_objects.csv'), np.array(x_vals), delimiter=',', fmt='%d')
        for item in export_dict.items():
            np.savetxt(os.path.join(output_dir, item[0] + '.csv'), item[1], delimiter=',', fmt='%1.4f')


def evaluate_all_folders_oxford(gt, plot_title, user_specified_result_dir=None, output_dir=None):
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
        ignore_dirs = ["HACS", "AVA"]
        for mydir in dirs:
            if mydir[0] == '.':
                continue  # Filter out `.DS_Store` and `._.DS_Store`
            if mydir in ignore_dirs:
                continue

            print("---Eval: %s ---" % mydir)
            user_specified_results = evaluate_folder(gt, os.path.join(user_specified_result_dir, mydir))
            export_dict[mydir] = user_specified_results

    x_vals = range(1001)

    # Plot everything specified via export_dict
    make_plot(export_dict, plot_title, x_vals)

    # Export figs, csv
    export_figs(export_dict, plot_title, output_dir, x_vals)


def eval_recall_oxford(output_dir):
    # +++ Most common categories +++
    # print("evaluating car, bike, person, bus:")
    print("evaluating coco 78 classes without hot_dog and oven:")
    exclude_classes = tuple(unknown_tao_ids)
    # ignored_seq = ("BDD", "Charades", "LaSOT", "YFCC100M", "HACS", "AVA")
    ignored_seq = ("HACS", "AVA")
    gt, n_gt_boxes, cat_id2tracks = load_gt(exclude_classes, ignored_seq, prefix_dir_name=FLAGS.labels)
    # gt, n_gt_boxes = load_gt_oxford(exclude_classes, prefix_dir_name=FLAGS.labels)

    evaluate_all_folders_oxford(gt, "COCO known classes (" + str(n_gt_boxes) + " bounding boxes)",
                                output_dir=output_dir,
                                user_specified_result_dir=FLAGS.evaluate_dir)

    # +++ "other" categories +++
    print("evaluating others:")
    exclude_classes = tuple(known_tao_ids)
    gt, n_gt_boxes, cat_id2tracks = load_gt(exclude_classes, ignored_seq, prefix_dir_name=FLAGS.labels)

    evaluate_all_folders_oxford(gt, "COCO unknown classes (" + str(n_gt_boxes) + " bounding boxes)",
                                output_dir=output_dir,
                                user_specified_result_dir=FLAGS.evaluate_dir)


def main():
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

        eval_recall_oxford(output_dir=output_dir)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    # Args
    parser.add_argument('--plot_output_dir', type=str, help='Plots output dir.')
    parser.add_argument('--evaluate_dir', type=str, help='Dir containing result files that you want to evaluate')
    parser.add_argument('--labels', type=str, default='',
                        help='Specify dir containing the labels')
    # parser.add_argument('--labels', type=str, default='oxford_labels',
    #                     help='Specify dir containing the labels')
    parser.add_argument('--do_not_timestamp', action='store_true', help='Dont timestamp output dirs')

    FLAGS = parser.parse_args()
    main()
