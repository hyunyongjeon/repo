# --------------------------------------------------------
# Tensorflow Faster R-CNN
# Licensed under The MIT License [see LICENSE for details]
# Written by Jiasen Lu, Jianwei Yang, based on code from Ross Girshick

# Modified by Peiliang Li for Stereo RCNN demo
# --------------------------------------------------------
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import _init_paths
import os
import sys
import numpy as np
import argparse
import shutil
import time
import cv2
import torch
from torch.autograd import Variable
import torch.nn as nn
import torch.optim as optim
import math as m
from roi_data_layer.roidb import combined_roidb
from roi_data_layer.roibatchLoader import roibatchLoader
from model.utils.config import cfg
from model.rpn.bbox_transform import clip_boxes
from model.roi_layers import nms
from model.rpn.bbox_transform import bbox_transform_inv, kpts_transform_inv, border_transform_inv
from model.utils.net_utils import save_net, load_net, vis_detections
from model.stereo_rcnn.resnet import resnet
from model.utils import kitti_utils
from model.utils import vis_3d_utils as vis_utils
from model.utils import box_estimator as box_estimator
from model.dense_align import dense_align

try:
    xrange          # Python 2
except NameError:
    xrange = range  # Python 3

def parse_args():
  """
  Parse input arguments
  """
  parser = argparse.ArgumentParser(description='Test the Stereo R-CNN network')

  parser.add_argument('--load_dir', dest='load_dir',
                      help='directory to load models', default="models_stereo",
                      type=str)
  parser.add_argument('--checkepoch', dest='checkepoch',
                      help='checkepoch to load network',
                      default=12, type=int)
  parser.add_argument('--checkpoint', dest='checkpoint',
                      help='checkpoint to load network',
                      default=6477, type=int)

  args = parser.parse_args()
  return args

if __name__ == '__main__':

  args = parse_args()

  np.random.seed(cfg.RNG_SEED)

  input_dir = args.load_dir + "/"
  if not os.path.exists(input_dir):
    raise Exception('There is no input directory for loading network from ' + input_dir)
  load_name = os.path.join(input_dir,
    'stereo_rcnn_{}_{}.pth'.format(args.checkepoch, args.checkpoint))
  kitti_classes = np.asarray(['__background__', 'Car'])

  # initilize the network here.
  stereoRCNN = resnet(kitti_classes, 101, pretrained=False)
  stereoRCNN.create_architecture()

  print("load checkpoint %s" % (load_name))
  checkpoint = torch.load(load_name)
  stereoRCNN.load_state_dict(checkpoint['model'])
  print('load model successfully!')

  with torch.no_grad():
    # initilize the tensor holder here.
    im_left_data = Variable(torch.FloatTensor(1).cuda())
    im_right_data = Variable(torch.FloatTensor(1).cuda())
    im_info = Variable(torch.FloatTensor(1).cuda())
    num_boxes = Variable(torch.LongTensor(1).cuda())
    gt_boxes = Variable(torch.FloatTensor(1).cuda())

    stereoRCNN.cuda()

    eval_thresh = 0.05
    vis_thresh = 0.7

    stereoRCNN.eval()
    
    # read data
    img_l_path = 'demo/left.png'
    img_r_path = 'demo/right.png'

    img_left = cv2.imread(img_l_path)
    img_right = cv2.imread(img_r_path)

    # rgb -> bgr
    img_left = img_left.astype(np.float32, copy=False)
    img_right = img_right.astype(np.float32, copy=False)

    img_left -= cfg.PIXEL_MEANS
    img_right -= cfg.PIXEL_MEANS

    im_shape = img_left.shape
    im_size_min = np.min(im_shape[0:2])
    im_scale = float(cfg.TRAIN.SCALES[0]) / float(im_size_min)

    img_left = cv2.resize(img_left, None, None, fx=im_scale, fy=im_scale,
                    interpolation=cv2.INTER_LINEAR)
    img_right = cv2.resize(img_right, None, None, fx=im_scale, fy=im_scale,
                    interpolation=cv2.INTER_LINEAR)
    
    info = np.array([[img_left.shape[0], img_left.shape[1], \
                         im_scale]], dtype=np.float32)
    
    img_left = torch.from_numpy(img_left)
    img_left = img_left.permute(2, 0, 1).unsqueeze(0).contiguous()

    img_right = torch.from_numpy(img_right)
    img_right = img_right.permute(2, 0, 1).unsqueeze(0).contiguous()

    info = torch.from_numpy(info)

    im_left_data.data.resize_(img_left.size()).copy_(img_left)
    im_right_data.data.resize_(img_right.size()).copy_(img_right)
    im_info.data.resize_(info.size()).copy_(info)
    
    det_tic = time.time()
    rois_left, rois_right, cls_prob, bbox_pred, bbox_pred_dim, kpts_prob,\
    left_prob, right_prob, rpn_loss_cls, rpn_loss_box_left_right,\
    RCNN_loss_cls, RCNN_loss_bbox, RCNN_loss_dim_orien, RCNN_loss_kpts, rois_label =\
    stereoRCNN(im_left_data, im_right_data, im_info, gt_boxes, gt_boxes,\
              gt_boxes, gt_boxes, gt_boxes, num_boxes)
    
    scores = cls_prob.data
    boxes_left = rois_left.data[:, :, 1:5]
    boxes_right = rois_right.data[:, :, 1:5]

    bbox_pred = bbox_pred.data
    box_delta_left = bbox_pred.new(bbox_pred.size()[1], 4*len(kitti_classes)).zero_()
    box_delta_right = bbox_pred.new(bbox_pred.size()[1], 4*len(kitti_classes)).zero_()

    for keep_inx in range(box_delta_left.size()[0]):
      box_delta_left[keep_inx, 0::4] = bbox_pred[0,keep_inx,0::6]
      box_delta_left[keep_inx, 1::4] = bbox_pred[0,keep_inx,1::6]
      box_delta_left[keep_inx, 2::4] = bbox_pred[0,keep_inx,2::6]
      box_delta_left[keep_inx, 3::4] = bbox_pred[0,keep_inx,3::6]

      box_delta_right[keep_inx, 0::4] = bbox_pred[0,keep_inx,4::6]
      box_delta_right[keep_inx, 1::4] = bbox_pred[0,keep_inx,1::6]
      box_delta_right[keep_inx, 2::4] = bbox_pred[0,keep_inx,5::6]
      box_delta_right[keep_inx, 3::4] = bbox_pred[0,keep_inx,3::6]

    box_delta_left = box_delta_left.view(-1,4)
    box_delta_right = box_delta_right.view(-1,4)

    dim_orien = bbox_pred_dim.data
    dim_orien = dim_orien.view(-1,5)

    kpts_prob = kpts_prob.data
    kpts_prob = kpts_prob.view(-1,4*cfg.KPTS_GRID)
    max_prob, kpts_delta = torch.max(kpts_prob,1)

    left_prob = left_prob.data
    left_prob = left_prob.view(-1,cfg.KPTS_GRID)
    _, left_delta = torch.max(left_prob,1)

    right_prob = right_prob.data
    right_prob = right_prob.view(-1,cfg.KPTS_GRID)
    _, right_delta = torch.max(right_prob,1)

    box_delta_left = box_delta_left * torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_STDS).cuda() \
                + torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_MEANS).cuda()
    box_delta_right = box_delta_right * torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_STDS).cuda() \
                + torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_MEANS).cuda()
    dim_orien = dim_orien * torch.FloatTensor(cfg.TRAIN.DIM_NORMALIZE_STDS).cuda() \
                + torch.FloatTensor(cfg.TRAIN.DIM_NORMALIZE_MEANS).cuda()


    box_delta_left = box_delta_left.view(1,-1,4*len(kitti_classes))
    box_delta_right = box_delta_right.view(1, -1,4*len(kitti_classes))
    dim_orien = dim_orien.view(1, -1, 5*len(kitti_classes))
    kpts_delta = kpts_delta.view(1, -1, 1)
    left_delta = left_delta.view(1, -1, 1)
    right_delta = right_delta.view(1, -1, 1)
    max_prob = max_prob.view(1, -1, 1)

    pred_boxes_left = bbox_transform_inv(boxes_left, box_delta_left, 1)
    pred_boxes_right = bbox_transform_inv(boxes_right, box_delta_right, 1)
    pred_kpts, kpts_type = kpts_transform_inv(boxes_left, kpts_delta,cfg.KPTS_GRID)
    pred_left = border_transform_inv(boxes_left, left_delta,cfg.KPTS_GRID)
    pred_right = border_transform_inv(boxes_left, right_delta,cfg.KPTS_GRID)

    pred_boxes_left = clip_boxes(pred_boxes_left, im_info.data, 1)
    pred_boxes_right = clip_boxes(pred_boxes_right, im_info.data, 1)

    pred_boxes_left /= im_info[0,2].data
    pred_boxes_right /= im_info[0,2].data
    pred_kpts /= im_info[0,2].data
    pred_left /= im_info[0,2].data
    pred_right /= im_info[0,2].data

    scores = scores.squeeze()
    pred_boxes_left = pred_boxes_left.squeeze()
    pred_boxes_right = pred_boxes_right.squeeze()

    pred_kpts = torch.cat((pred_kpts, kpts_type, max_prob, pred_left, pred_right),2)
    pred_kpts = pred_kpts.squeeze()
    dim_orien = dim_orien.squeeze()

    det_toc = time.time()
    detect_time = det_toc - det_tic

    calib = kitti_utils.read_obj_calibration('demo/calib.txt')

    im2show_left = np.copy(cv2.imread(img_l_path))
    im2show_right = np.copy(cv2.imread(img_r_path))
    
    pointcloud = kitti_utils.get_point_cloud('demo/lidar.bin', calib)
    im_box = vis_utils.vis_lidar_in_bev(pointcloud, width=im2show_left.shape[0]*2)

    for j in xrange(1, len(kitti_classes)):
      inds = torch.nonzero(scores[:,j] > eval_thresh).view(-1)
      # if there is det
      if inds.numel() > 0:
        cls_scores = scores[:,j][inds]
        _, order = torch.sort(cls_scores, 0, True)

        cls_boxes_left = pred_boxes_left[inds][:, j * 4:(j + 1) * 4]
        cls_boxes_right = pred_boxes_right[inds][:, j * 4:(j + 1) * 4]
        cls_dim_orien = dim_orien[inds][:, j * 5:(j + 1) * 5]
        
        cls_kpts = pred_kpts[inds]

        cls_dets_left = torch.cat((cls_boxes_left, cls_scores.unsqueeze(1)), 1)
        cls_dets_right = torch.cat((cls_boxes_right, cls_scores.unsqueeze(1)), 1)

        cls_dets_left = cls_dets_left[order]
        cls_dets_right = cls_dets_right[order]
        cls_dim_orien = cls_dim_orien[order]
        cls_kpts = cls_kpts[order] 

        keep = nms(cls_boxes_left[order, :], cls_scores[order], cfg.TEST.NMS)
        keep = keep.view(-1).long()
        cls_dets_left = cls_dets_left[keep]
        cls_dets_right = cls_dets_right[keep]
        cls_dim_orien = cls_dim_orien[keep]
        cls_kpts = cls_kpts[keep]

        # optional operation, can check the regressed borderline keypoint using 2D box inference
        infered_kpts = kitti_utils.infer_boundary(im2show_left.shape, cls_dets_left.cpu().numpy())
        infered_kpts = torch.from_numpy(infered_kpts).type_as(cls_dets_left)
        for detect_idx in range(cls_dets_left.size()[0]):
          if cls_kpts[detect_idx,4] - cls_kpts[detect_idx,3] < \
              0.5*(infered_kpts[detect_idx,1]-infered_kpts[detect_idx,0]):
            cls_kpts[detect_idx,3:5] = infered_kpts[detect_idx]

        im2show_left = vis_detections(im2show_left, kitti_classes[j], \
                        cls_dets_left.cpu().numpy(), vis_thresh, cls_kpts.cpu().numpy())
        im2show_right = vis_detections(im2show_right, kitti_classes[j], \
                        cls_dets_right.cpu().numpy(), vis_thresh) 

        # read intrinsic
        f = calib.p2[0,0]
        cx, cy = calib.p2[0,2], calib.p2[1,2]
        bl = (calib.p2[0,3] - calib.p3[0,3])/f

        boxes_all = cls_dets_left.new(0,5)
        kpts_all = cls_dets_left.new(0,5)
        poses_all = cls_dets_left.new(0,8)

        solve_tic = time.time()
        for detect_idx in range(cls_dets_left.size()[0]):
          if cls_dets_left[detect_idx, -1] > eval_thresh:
            box_left = cls_dets_left[detect_idx,0:4].cpu().numpy()  # based on origin image
            box_right = cls_dets_right[detect_idx,0:4].cpu().numpy() 
            kpts_u = cls_kpts[detect_idx,0]
            dim = cls_dim_orien[detect_idx,0:3].cpu().numpy()
            sin_alpha = cls_dim_orien[detect_idx,3]
            cos_alpha = cls_dim_orien[detect_idx,4]
            alpha = m.atan2(sin_alpha, cos_alpha)
            status, state = box_estimator.solve_x_y_z_theta_from_kpt(im2show_left.shape, calib, alpha, \
                                          dim, box_left, box_right, cls_kpts[detect_idx].cpu().numpy())
            if status > 0: # not faild
              poses = im_left_data.data.new(8).zero_()
              xyz = np.array([state[0], state[1], state[2]])
              theta = state[3]
              poses[0], poses[1], poses[2], poses[3], poses[4], poses[5], poses[6], poses[7] = \
                xyz[0], xyz[1], xyz[2], float(dim[0]), float(dim[1]), float(dim[2]), theta, alpha

              boxes_all = torch.cat((boxes_all,cls_dets_left[detect_idx,0:5].unsqueeze(0)),0)
              kpts_all = torch.cat((kpts_all,cls_kpts[detect_idx].unsqueeze(0)),0)
              poses_all = torch.cat((poses_all,poses.unsqueeze(0)),0)
        
        if boxes_all.dim() > 0:
          # solve disparity by dense alignment (enlarged image)
          succ, dis_final = dense_align.align_parallel(calib, im_info.data[0,2], \
                                              im_left_data.data, im_right_data.data, \
                                              boxes_all[:,0:4], kpts_all, poses_all[:,0:7])
          
          # do 3D rectify using the aligned disparity
          for solved_idx in range(succ.size(0)):
            if succ[solved_idx] > 0: # succ
              box_left = boxes_all[solved_idx,0:4].cpu().numpy()
              score = boxes_all[solved_idx,4].cpu().numpy()
              dim = poses_all[solved_idx,3:6].cpu().numpy()
              state_rect, z = box_estimator.solve_x_y_theta_from_kpt(im2show_left.shape, calib, \
                                          poses_all[solved_idx,7].cpu().numpy(), dim, box_left, \
                                          dis_final[solved_idx].cpu().numpy(), kpts_all[solved_idx].cpu().numpy())
              xyz = np.array([state_rect[0], state_rect[1], z])
              theta = state_rect[2]

              if score > vis_thresh:
                im_box = vis_utils.vis_box_in_bev(im_box, xyz, dim, theta, width=im2show_left.shape[0]*2)
                im2show_left = vis_utils.vis_single_box_in_img(im2show_left, calib, xyz, dim, theta)

        solve_time = time.time() - solve_tic

    sys.stdout.write('demo mode (Press Esc to exit!) \r'\
                      .format(detect_time, solve_time))

    im2show = np.concatenate((im2show_left, im2show_right), axis=0)
    im2show = np.concatenate((im2show, im_box), axis=1)
    cv2.imshow('result', im2show)

    k = cv2.waitKey(-1)
    if k == 27:    # Esc key to stop
        print('exit!')
        sys.exit()





