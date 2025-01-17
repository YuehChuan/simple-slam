#!/usr/bin/env python

import argparse
import os
import sys
import time

import cv2
import numpy as np
import skimage

import frame as fm
import helpers as helper
from display import Display
from slam_map import SlamMap, SlamPoint

np.set_printoptions(suppress=True)


class Slam(object):
    """Main functional class containing the SlamMap"""

    def __init__(self, K, width, height):
        self.slam_map = SlamMap()
        self.K = K
        self.width = width
        self.height = height

    def matchFrame(self, frame1, frame2):
        """ Matches frame1 and frame2
        :frame1:
        :frame2:
        :returns: TODO

        """
        matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
        matches = matcher.knnMatch(frame1.descriptors, frame2.descriptors, k=2)
        ratio = 0.65
        match_idx1, match_idx2 = [], []
        for m1, m2 in matches:
            if (m1.distance < ratio*m2.distance) and (m1.distance < 32):
                if m1.queryIdx not in match_idx1 and m1.trainIdx not in match_idx2:
                    match_idx1.append(m1.queryIdx)
                    match_idx2.append(m1.trainIdx)

        # We need at least 8 matches for calculating Fundamental matrix
        assert(len(match_idx1) >= 8)
        assert(len(match_idx2) >= 8)

        assert(len(set(match_idx1)) == len(match_idx1))
        assert(len(set(match_idx2)) == len(match_idx2))

        match_idx1, match_idx2 = np.asarray(match_idx1), np.asarray(match_idx2)

        frame1_pts_un = frame1.keypoints_un[match_idx1, :]
        frame2_pts_un = frame2.keypoints_un[match_idx2, :]

        F, inlier_mask = cv2.findFundamentalMat(
            frame1_pts_un, frame2_pts_un, cv2.RANSAC, 0.1, 0.99)
        E = self.K.T @ F @ self.K
        inlier_mask = inlier_mask.astype(bool).squeeze()
        print("Matches: matches = {}, inliers = {}".format(
            len(matches), inlier_mask.shape[0]))
        print("Fundamental matrix: \n{}\n".format(F))

        _, R, t, _ = cv2.recoverPose(E, frame1_pts_un, frame2_pts_un, self.K)
        pose = np.eye(4)
        pose[0:3, 0:3] = R
        pose[0:3, 3] = t.squeeze()
        return pose, match_idx1[inlier_mask], match_idx2[inlier_mask]

    def searchByProjection(self, curr_frame):
        """TODO: Docstring for search.
        :returns: TODO

        """
        map_points = [p.point for p in self.slam_map.points]
        proj_points, mask = helper.projectPoints(
            map_points, curr_frame.pose, self.width, self.height)
        proj_points = helper.denormalizePoints(proj_points, self.K)
        # obtain projection point descriptors for matching
        proj_keypoints = [cv2.KeyPoint(
            x=proj_point[0], y=proj_point[1], _size=10) for proj_point in proj_points]
        print(len(proj_keypoints))
        orb = cv2.ORB_create(1000)
        _, proj_descriptors = orb.compute(curr_frame.image, proj_keypoints)
        bf = cv2.BFMatcher(cv2.NORM_HAMMING)
        matches = bf.knnMatch(proj_descriptors, curr_frame.descriptors, k=2)
        ratio = 0.65
        proj_matches = []
        for m1, m2 in matches:
            if (m1.distance < ratio*m2.distance) and (m1.distance < 64):
                proj_matches.append(m1.trainIdx)

        print("Projected matches: ", len(proj_matches))
        sbp_count = 0
        for i, slam_point in enumerate(self.slam_map.points):
            # if the proj_point is not inlier
            if not mask[i]:
                continue
            if curr_frame in slam_point.frames:
                continue
            # if this map point is matched with frame
            j = 0
            for m_idx in proj_matches:
                # If the proj_match points are close to existing keypoints in the frame
                dist = slam_point.orb_distance(curr_frame.descriptors[m_idx])
                if dist < 32:
                    print(dist)
                    print(curr_frame.keypoints[m_idx])
                    j += 1
                    if curr_frame.map_points[m_idx] is None and curr_frame not in slam_point.frames:
                        slam_point.addObservation(curr_frame, m_idx)
                        sbp_count += 1
        return proj_matches, sbp_count

    def processFrame(self, image):
        """

        :image: TODO
        :slam_map: TODO
        :returns: TODO

        """
        start_time = time.time()
        frame = fm.Frame(image, self.K)
        self.slam_map.addFrame(frame)

        if frame.id == 0:
            return

        curr_frame = self.slam_map.frames[-1]
        prev_frame = self.slam_map.frames[-2]

        relative_pose, match_idx1, match_idx2 = self.matchFrame(prev_frame, curr_frame)

        # Compose the pose of the frame (this forms our pose estimate for optimize)
        curr_frame.pose = relative_pose @ prev_frame.pose
        print("Previous frame pose: \n{}\n".format(prev_frame.pose))
        print("Relative pose: \n{}\n".format(relative_pose))
        print("Current Pose before optimize: \n{}\n".format(
            np.linalg.inv(curr_frame.pose)))
        P1 = prev_frame.pose[0:3, :]
        P2 = curr_frame.pose[0:3, :]

        # If a point has been observed in previous frame, add a corresponding observation even in the current frame
        for i, idx in enumerate(match_idx1):
            if prev_frame.map_points[idx] is not None and curr_frame.map_points[match_idx2[i]] is None:
                prev_frame.map_points[idx].addObservation(
                    curr_frame, match_idx2[i])

        # If the map contains points
        sbp_count = 0
        if len(self.slam_map.points) > 0:
            # Optimize the pose only by keeping the map_points fixed
            reproj_error = self.slam_map.optimize(
                local_window=2, fix_points=True)
            print("Reprojection error after pose optimize: {}".format(reproj_error))
            # proj_matches, sbp_count = self.searchByProjection(curr_frame)

        # Triangulate only those points that were not found by searchByProjection (innovative points)
        points3d, _ = helper.triangulate(
            P1, prev_frame.keypoints[match_idx1], P2, curr_frame.keypoints[match_idx2])
        remaining_point_mask = np.array(
            [curr_frame.map_points[i] is None for i in match_idx2])

        add_count = 0
        for i, point3d in enumerate(points3d):
            if not remaining_point_mask[i]:
                continue

            # Check if 3D point is in front of both frames
            point4d_homo = np.hstack((point3d, 1))
            point1 = prev_frame.pose @ point4d_homo
            point2 = curr_frame.pose @ point4d_homo
            if point1[2] < 0 and point2[2] < 0:
                continue

            proj_point1, _ = helper.projectPoints(
                point3d, prev_frame.pose, self.width, self.height)
            proj_point2, _ = helper.projectPoints(
                point3d, curr_frame.pose, self.width, self.height)
            # print("Proj points", proj_point1, proj_point2)
            # print("Frame keypoints", prev_frame.keypoints[match_idx1[i]], curr_frame.keypoints[match_idx2[i]])
            err1 = np.sum(
                np.square(proj_point1-prev_frame.keypoints[match_idx1[i]]))
            err2 = np.sum(
                np.square(proj_point2-curr_frame.keypoints[match_idx2[i]]))
            # print(err1, err2)
            if err1 > 1 or err2 > 1:
                continue

            color = image[int(np.round(curr_frame.keypoints[match_idx2[i]][1])), int(
                np.round(curr_frame.keypoints[match_idx2[i]][0]))]
            point = SlamPoint(points3d[i], color)
            point.addObservation(prev_frame, match_idx1[i])
            point.addObservation(curr_frame, match_idx2[i])
            self.slam_map.addPoint(point)
            add_count += 1

        if frame.id > 4 and frame.id % 5 == 0:
            self.slam_map.optimize()
        print("Added points in map: {} Search By projection: {}".format(
            add_count, sbp_count))
        print("Map: {} points, {} frames".format(
            len(self.slam_map.points), len(self.slam_map.frames)))
        print("Time:    {:.2f} ms".format((time.time()-start_time)*1000.0))
        # wait = input("Enter to continue")

        return frame.drawFrame(prev_frame.keypoints_un[match_idx1], curr_frame.keypoints_un[match_idx2])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simple slam pipeline")
    parser.add_argument("-i", "--input", required=True,
                        help="Input video file")
    parser.add_argument("-f", "--focal-length", type=int, required=True,
                        help="Estimated focal length for scale initialization")

    args = parser.parse_args()

    capture = cv2.VideoCapture(args.input)
    if capture.isOpened():
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = capture.get(cv2.CAP_PROP_FPS)
        no_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))

        if width > 1024:
            aspect = 1024/width
            height = int(height * aspect)
            width = 1024

        print("Video parameters: width {}, height {}, fps {}".format(
            width, height, fps))

    focal_length = args.focal_length
    K = np.array([[focal_length, 0.0,  width//2],
                  [0.0, focal_length, height//2],
                  [0.0,          0.0,      1.0]])

    # Initialize Slam instance
    slam = Slam(K, width, height)
    disp = Display()

    i = 0
    while capture.isOpened():
        ret, frame = capture.read()

        if ret == True:
            print("---------- Frame {} / {} ----------".format(i, no_frames))
            frame = cv2.resize(frame, (width, height))
            processed_frame = slam.processFrame(frame)
            if i > 0:
                cv2.namedWindow('frame', cv2.WINDOW_NORMAL)
                cv2.imshow('frame', processed_frame)
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break
            disp.update(slam.slam_map)
        else:
            break
        i += 1
    disp.finish()
    capture.release()
    cv2.destroyAllWindows()
    cv2.waitKey(0)

