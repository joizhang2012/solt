from collections import OrderedDict
from abc import ABCMeta, abstractmethod
import cv2
import numpy as np

from .data import DataContainer, img_shape_checker, KeyPoints
from .constants import allowed_interpolations, allowed_paddings


class BaseTransform(metaclass=ABCMeta):
    """
    Transformation abstract class.

    """
    def __init__(self, p=0.5):
        """
        Constructor.

        Parameters
        ----------
        p : probability of executing this transform

        """
        self.p = p
        self.state_dict = {'use': False}

    def serialize(self, include_state=False):
        """
        Method returns an ordered dict, describing the object.

        Parameters
        ----------
        include_state : bool
            Whether to include a self.state_dict into the result. Mainly useful for debug.
        Returns
        -------
        out : OrderedDict
            OrderedDict, ready for json serialization.

        """
        if not include_state:
            d = dict(
                map(lambda item: (item[0].split('__')[-1], item[1]),
                    filter(lambda item: item[0] != 'state_dict',
                           self.__dict__.items())
                    )
            )
        else:
            d = dict(map(lambda item: (item[0].split('__')[-1], item[1]), self.__dict__.items()))

        # the method must return the result always in the same order
        return OrderedDict(sorted(d.items()))

    def use_transform(self):
        """
        Method to randomly determine whether to use this transform.

        Returns
        -------
        out : bool
            Boolean flag. True if the transform is used.
        """
        if np.random.rand() < self.p:
            self.state_dict['use'] = True
            return True

        self.state_dict['use'] = False
        return False

    @abstractmethod
    def sample_transform(self):
        """
        Abstract method. Must be implemented in the child classes

        Returns
        -------
        None

        """
        pass

    def apply(self, data):
        """
        Applies transformation to a DataContainer items depending on the type.

        Parameters
        ----------
        data : DataContainer
            Data to be augmented

        Returns
        -------
        out : DataContainer
            Result

        """
        result = []
        types = []
        for i, (item, t) in enumerate(data):
            if t == 'I':  # Image
                tmp_item = self._apply_img(item)
            elif t == 'M':  # Mask
                tmp_item = self._apply_mask(item)
            elif t == 'P':  # Points
                tmp_item = self._apply_pts(item)
            else:  # Labels
                tmp_item = self._apply_labels(item)

            types.append(t)
            result.append(tmp_item)

        return DataContainer(data=tuple(result), fmt=''.join(types))

    def __call__(self, data):
        """
        Applies the transform to a DataContainer

        Parameters
        ----------
        data : DataContainer
            Data to be augmented

        Returns
        -------
        out : DataContainer
            Result

        """
        if self.use_transform():
            self.sample_transform()
            return self.apply(data)
        else:
            return data

    @abstractmethod
    def _apply_img(self, img):
        """
        Abstract method, which determines the transform's behaviour when it is applied to images HxWxC.

        Parameters
        ----------
        img : ndarray
            Image to be augmented

        Returns
        -------
        out : ndarray

        """
        pass

    @abstractmethod
    def _apply_mask(self, mask):
        """
        Abstract method, which determines the transform's behaviour when it is applied to masks HxW.

        Parameters
        ----------
        mask : ndarray
            Mask to be augmented

        Returns
        -------
        out : ndarray
            Result

        """
        pass

    @abstractmethod
    def _apply_labels(self, labels):
        """
        Abstract method, which determines the transform's behaviour when it is applied to labels (e.g. label smoothing)

        Parameters
        ----------
        labels : ndarray
            Array of labels.

        Returns
        -------
        out : ndarray
            Result

        """
        pass

    @abstractmethod
    def _apply_pts(self, pts):
        """
        Abstract method, which determines the transform's behaviour when it is applied to keypoints.

        Parameters
        ----------
        pts : KeyPoints
            Keypoints object

        Returns
        -------
        out : KeyPoints
            Result

        """
        pass


class MatrixTransform(BaseTransform):
    """
    Matrix Transform abstract class. (Affine and Homography).
    Does all the transforms around the image /  center.

    """
    def __init__(self, interpolation='bilinear', padding='z', p=0.5):
        if padding is not None:
            assert padding in allowed_paddings
        # TODO: interpolation for each item within data container
        assert interpolation in allowed_interpolations
        super(MatrixTransform, self).__init__(p=p)
        self.padding = padding
        self.interpolation = interpolation
        self.state_dict = {'transform_matrix': np.eye(3)}

    def fuse_with(self, trf):
        """
        Takes a transform an performs a matrix fusion. This is useful to optimize the computations

        Parameters
        ----------
        trf : MatrixTransform

        """
        assert self.state_dict is not None
        assert trf.state_dict is not None

        if trf.padding is not None:
            self.padding = trf.padding
        self.interpolation = trf.interpolation

        self.state_dict['transform_matrix'] = trf.state_dict['transform_matrix'] @ self.state_dict ['transform_matrix']

    @abstractmethod
    def sample_transform(self):
        """
        Abstract method. Must be implemented in the child classes

        Returns
        -------
        None

        """
        pass

    @staticmethod
    def correct_for_frame_change(M, W, H):
        """
        Method takes a matrix transform, and modifies its origin.

        Parameters
        ----------
        M : ndarray
            Transform (3x3) matrix
        W : int
            Width of the coordinate frame
        H : int
            Height of the coordinate frame
        Returns
        -------
        out : ndarray
            Modified Transform matrix

        """
        # First we correct the transformation so that it is performed around the origin
        origin = [(W-1) // 2, (H-1) // 2]
        T_origin = np.array([1, 0, -origin[0],
                             0, 1, -origin[1],
                             0, 0, 1]).reshape((3, 3))

        T_origin_back = np.array([1, 0, origin[0],
                                  0, 1, origin[1],
                                  0, 0, 1]).reshape((3, 3))

        # TODO: Check whether translation works and use this matrix if possible
        T_initial = np.array([1, 0, M[0, 2],
                             0, 1, M[1, 2],
                             0, 0, 1]).reshape((3, 3))

        M = T_origin_back @ M @ T_origin

        # Now, if we think of scaling, rotation and translation, the image gets increased when we
        # apply any transform.

        # This is needed to recalculate the size of the image after the transformation.
        # The core idea is to transform the coordinate grid
        # left top, left bottom, right bottom, right top
        coord_frame = np.array([[0, 0, 1], [0, H, 1], [W, H, 1], [W, 0, 1]])
        new_frame = np.dot(M, coord_frame.T).T
        new_frame[:, 0] /= new_frame[:, -1]
        new_frame[:, 1] /= new_frame[:, -1]
        new_frame = new_frame[:, :-1]
        # Computing the new coordinates

        # If during the transform, we obtained negativa coordinates, we have to move to the origin
        if np.any(new_frame[:, 0] < 0):
            new_frame[:, 0] += abs(new_frame[:, 0].min())
        if np.any(new_frame[:, 1] < 0):
            new_frame[:, 1] += abs(new_frame[:, 1].min())
        # In case of scaling the coordinate_frame, we need to move back to the origin
        new_frame[:, 0] -= new_frame[:, 0].min()
        new_frame[:, 1] -= new_frame[:, 1].min()

        W_new = int(np.round(new_frame[:, 0].max()))
        H_new = int(np.round(new_frame[:, 1].max()))

        M[0, -1] += W_new//2-origin[0]
        M[1, -1] += H_new//2-origin[1]

        return M, W_new, H_new

    @img_shape_checker
    def _apply_img(self, img):
        """
        Applies a matrix transform to an image.
        If padding is None, the default behavior (zero padding) is expected.

        Parameters
        ----------
        img : ndarray
            Input Image

        Returns
        -------
        out : ndarray
            Output Image

        """
        M = self.state_dict['transform_matrix']
        M, W_new, H_new = MatrixTransform.correct_for_frame_change(M, img.shape[1], img.shape[0])

        interp = allowed_interpolations[self.interpolation]
        if self.padding == 'z' or self.padding is None:
            return cv2.warpPerspective(img, M , (W_new, H_new), interp,
                                       borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        elif self.padding == 'r':
            return cv2.warpPerspective(img, M, (W_new, H_new), interp,
                                       borderMode=cv2.BORDER_REFLECT)
        else:
            raise NotImplementedError

    def _apply_mask(self, mask):
        """
        Abstract method, which determines the transform's behaviour when it is applied to masks HxW.
        If padding is None, the default behavior (zero padding) is expected.

        Parameters
        ----------
        mask : ndarray
            Mask to be augmented

        Returns
        -------
        out : ndarray
            Result

        """
        # X, Y coordinates
        M = self.state_dict['transform_matrix']
        M, W_new, H_new = MatrixTransform.correct_for_frame_change(M, mask.shape[1], mask.shape[0])
        interp = allowed_interpolations[self.interpolation]
        if self.padding == 'z' or self.padding is None:
            return cv2.warpPerspective(mask, M , (W_new, H_new), interp,
                                       borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        elif self.padding == 'r':
            return cv2.warpPerspective(mask, M, (W_new, H_new), interp,
                                       borderMode=cv2.BORDER_REFLECT)
        else:
            raise NotImplementedError

    def _apply_labels(self, labels):
        """
        Transform application to labels. Simply returns them.

        Parameters
        ----------
        labels : ndarray
            Array of labels.

        Returns
        -------
        out : ndarray
            Result

        """
        return labels

    def _apply_pts(self, pts):
        """
        Abstract method, which determines the transform's behaviour when it is applied to keypoints.

        Parameters
        ----------
        pts : KeyPoints
            Keypoints object

        Returns
        -------
        out : KeyPoints
            Result

        """
        if self.padding == 'r':
            raise ValueError('Cannot apply transform to keypoints with reflective padding!')

        pts_data = pts.data.copy()
        M = self.state_dict['transform_matrix']
        M, W_new, H_new = MatrixTransform.correct_for_frame_change(M, pts.W, pts.H)

        pts_data = np.hstack((pts_data, np.ones((pts_data.shape[0], 1))))
        pts_data = np.dot(M, pts_data.T).T

        pts_data[:, 0] /= pts_data[:, 2]
        pts_data[:, 1] /= pts_data[:, 2]

        return KeyPoints(pts_data[:, :-1], H_new, W_new)