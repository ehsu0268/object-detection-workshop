import numpy as np
import tensorflow as tf

from PIL import Image, ImageDraw
from tensorflow.contrib import slim
from tensorflow.contrib.slim.nets import resnet_v1


_R_MEAN = 123.68
_G_MEAN = 116.78
_B_MEAN = 103.94


def draw_rectangle(draw, coordinates, color, width=1):
    fill = tuple(color + [40])
    outline = tuple(color + [255])

    for i in range(width):
        coords = [
            coordinates[0] - i,
            coordinates[1] - i,
            coordinates[2] + i,
            coordinates[3] + i,
        ]
        if i == 0:
            # draw.rectangle(coords, fill=fill, outline=outline)
            draw.rectangle(coords, outline=outline)
        else:
            draw.rectangle(coords, outline=outline)


def draw_bboxes(image, objects):
    # Open as 'RGBA' in order to draw translucent boxes.
    draw = ImageDraw.Draw(image, 'RGBA')
    for obj in objects:
        color = [255, 0, 0]
        draw_rectangle(draw, obj, color, width=2)


def get_width_upright(bboxes):
    with tf.name_scope('BoundingBoxTransform/get_width_upright'):
        bboxes = tf.cast(bboxes, tf.float32)
        x1, y1, x2, y2 = tf.split(bboxes, 4, axis=1)
        width = x2 - x1 + 1.
        height = y2 - y1 + 1.

        # Calculate up right point of bbox (urx = up right x)
        urx = x1 + .5 * width
        ury = y1 + .5 * height

        return width, height, urx, ury


def change_order(bboxes):
    """Change bounding box encoding order.

    TensorFlow works with the (y_min, x_min, y_max, x_max) order while we work
    with the (x_min, y_min, x_max, y_min).

    While both encoding options have its advantages and disadvantages we
    decided to use the (x_min, y_min, x_max, y_min), forcing use to switch to
    TensorFlow's every time we want to use a std function that handles bounding
    boxes.

    Args:
        bboxes: A Tensor of shape (total_bboxes, 4)

    Returns:
        bboxes: A Tensor of shape (total_bboxes, 4) with the order swaped.
    """
    with tf.name_scope('BoundingBoxTransform/change_order'):
        first_min, second_min, first_max, second_max = tf.unstack(
            bboxes, axis=1
        )
        bboxes = tf.stack(
            [second_min, first_min, second_max, first_max], axis=1
        )
        return bboxes


def encode(bboxes, gt_boxes, variances=None):
    with tf.name_scope('BoundingBoxTransform/encode'):
        (bboxes_width, bboxes_height,
         bboxes_urx, bboxes_ury) = get_width_upright(bboxes)

        (gt_boxes_width, gt_boxes_height,
         gt_boxes_urx, gt_boxes_ury) = get_width_upright(gt_boxes)

        if variances is None:
            variances = [1., 1.]

        targets_dx = (gt_boxes_urx - bboxes_urx)/(bboxes_width * variances[0])
        targets_dy = (gt_boxes_ury - bboxes_ury)/(bboxes_height * variances[0])

        targets_dw = tf.log(gt_boxes_width / bboxes_width) / variances[1]
        targets_dh = tf.log(gt_boxes_height / bboxes_height) / variances[1]

        targets = tf.concat(
            [targets_dx, targets_dy, targets_dw, targets_dh], axis=1)

        return targets


def decode(roi, deltas, variances=None):
    with tf.name_scope('BoundingBoxTransform/decode'):
        (roi_width, roi_height,
         roi_urx, roi_ury) = get_width_upright(roi)

        dx, dy, dw, dh = tf.split(deltas, 4, axis=1)

        if variances is None:
            variances = [1., 1.]

        pred_ur_x = dx * roi_width * variances[0] + roi_urx
        pred_ur_y = dy * roi_height * variances[0] + roi_ury
        pred_w = tf.exp(dw * variances[1]) * roi_width
        pred_h = tf.exp(dh * variances[1]) * roi_height

        bbox_x1 = pred_ur_x - 0.5 * pred_w
        bbox_y1 = pred_ur_y - 0.5 * pred_h

        # This -1. extra is different from reference implementation.
        bbox_x2 = pred_ur_x + 0.5 * pred_w - 1.
        bbox_y2 = pred_ur_y + 0.5 * pred_h - 1.

        bboxes = tf.concat(
            [bbox_x1, bbox_y1, bbox_x2, bbox_y2], axis=1)

        return bboxes


def generate_anchors_reference(base_size, aspect_ratios, scales):
    """Generate base anchor to be used as reference of generating all anchors.

    Anchors vary only in width and height. Using the base_size and the
    different ratios we can calculate the wanted widths and heights.

    Scales apply to area of object.

    Args:
        base_size (int): Base size of the base anchor (square).
        aspect_ratios: Ratios to use to generate different anchors. The ratio
            is the value of height / width.
        scales: Scaling ratios applied to area.

    Returns:
        anchors: Numpy array with shape (total_aspect_ratios * total_scales, 4)
            with the corner points of the reference base anchors using the
            convention (x_min, y_min, x_max, y_max).
    """
    scales_grid, aspect_ratios_grid = np.meshgrid(scales, aspect_ratios)
    base_scales = scales_grid.reshape(-1)
    base_aspect_ratios = aspect_ratios_grid.reshape(-1)

    aspect_ratio_sqrts = np.sqrt(base_aspect_ratios)
    heights = base_scales * aspect_ratio_sqrts * base_size
    widths = base_scales / aspect_ratio_sqrts * base_size

    # Center point has the same X, Y value.
    center_xy = 0

    # Create anchor reference.
    anchors = np.column_stack([
        center_xy - (widths - 1) / 2,
        center_xy - (heights - 1) / 2,
        center_xy + (widths - 1) / 2,
        center_xy + (heights - 1) / 2,
    ])

    real_heights = (anchors[:, 3] - anchors[:, 1]).astype(np.int)
    real_widths = (anchors[:, 2] - anchors[:, 0]).astype(np.int)

    if (real_widths == 0).any() or (real_heights == 0).any():
        raise ValueError(
            'base_size {} is too small for aspect_ratios and scales.'.format(
                base_size
            )
        )

    return anchors


def generate_anchors(feature_map_shape):
    """Generate anchor for an image.

    Using the feature map, the output of the pretrained network for an
    image, and the anchor_reference generated using the anchor config
    values. We generate a list of anchors.

    Anchors are just fixed bounding boxes of different ratios and sizes
    that are uniformly generated throught the image.

    Args:
        feature_map_shape: Shape of the convolutional feature map used as
            input for the RPN. Should be (batch, height, width, depth).

    Returns:
        all_anchors: A flattened Tensor with all the anchors of shape
            `(num_anchors_per_points * feature_width * feature_height, 4)`
            using the (x1, y1, x2, y2) convention.
    """
    anchor_base_size = 256
    anchor_ratios = [0.5, 1, 2]
    anchor_scales = [0.125, 0.25, 0.5, 1, 2]
    anchor_stride = 16

    anchor_reference = generate_anchors_reference(
        anchor_base_size, anchor_ratios, anchor_scales
    )
    with tf.variable_scope('generate_anchors'):
        grid_width = feature_map_shape[2]  # width
        grid_height = feature_map_shape[1]  # height
        shift_x = tf.range(grid_width) * anchor_stride
        shift_y = tf.range(grid_height) * anchor_stride
        shift_x, shift_y = tf.meshgrid(shift_x, shift_y)

        shift_x = tf.reshape(shift_x, [-1])
        shift_y = tf.reshape(shift_y, [-1])

        shifts = tf.stack(
            [shift_x, shift_y, shift_x, shift_y],
            axis=0
        )

        shifts = tf.transpose(shifts)
        # Shifts now is a (H x W, 4) Tensor

        # Expand dims to use broadcasting sum.
        all_anchors = (
            np.expand_dims(anchor_reference, axis=0) +
            tf.expand_dims(shifts, axis=1)
        )

        # Flatten
        all_anchors = tf.reshape(
            all_anchors, (-1, 4)
        )
        return all_anchors


def build_rpn(inputs):
    # TODO: Substract means and normalize.
    inputs = inputs - [_R_MEAN, _G_MEAN, _B_MEAN]

    im_shape = tf.shape(inputs)[1:3]

    # Build the base network.
    with slim.arg_scope(resnet_v1.resnet_utils.resnet_arg_scope()):
        _, endpoints = resnet_v1.resnet_v1_101(
            inputs,
            is_training=False,
            num_classes=None,
            global_pool=False,
            output_stride=16,
        )

    feature_map = endpoints['resnet_v1_101/block3']

    # Generate the anchors.
    all_anchors = generate_anchors(tf.shape(feature_map))
    # TODO: Get from anchor_reference.shape[0].
    num_anchors = 3 * 5  # 3 ratios, 5 scales.

    # Build the RPN.
    rpn_conv = tf.layers.conv2d(
        feature_map,
        filters=512,
        kernel_size=[3, 3],
        activation=tf.nn.relu6,
        kernel_initializer=tf.random_normal_initializer(mean=0.0, stddev=0.01),
        kernel_regularizer=tf.contrib.layers.l2_regularizer(0.0005),
        padding='same',
        name='rpn/conv',
    )

    rpn_cls = tf.layers.conv2d(
        rpn_conv,
        filters=num_anchors * 2,
        kernel_size=[1, 1],
        kernel_initializer=tf.random_normal_initializer(mean=0.0, stddev=0.01),
        kernel_regularizer=tf.contrib.layers.l2_regularizer(0.0005),
        name='rpn/cls_conv',
    )
    rpn_cls_score = tf.reshape(rpn_cls, [-1, 2])  # TODO: Why two classes?
    rpn_cls_prob = tf.nn.softmax(rpn_cls_score)

    rpn_bbox = tf.layers.conv2d(
        rpn_conv,
        filters=num_anchors * 4,
        kernel_size=[1, 1],
        kernel_initializer=(
            tf.random_normal_initializer(mean=0.0, stddev=0.001)
        ),
        kernel_regularizer=tf.contrib.layers.l2_regularizer(0.0005),
        name='rpn/bbox_conv',
    )
    rpn_bbox_pred = tf.reshape(rpn_bbox, [-1, 4])

    # Generate proposals from the RPN's output.

    # Get the (positive-object) scores from the RPN.
    # TODO: Can this be avoided?
    all_scores = tf.reshape(rpn_cls_prob[:, 1], [-1])

    # Filter out outside anchors.
    with tf.name_scope('filter_outside_anchors'):
        (
            x_min_anchor, y_min_anchor, x_max_anchor, y_max_anchor
        ) = tf.unstack(all_anchors, axis=1)

        anchor_filter = tf.logical_and(
            tf.logical_and(
                tf.greater_equal(x_min_anchor, 0),
                tf.greater_equal(y_min_anchor, 0)
            ),
            tf.logical_and(
                tf.less(x_max_anchor, im_shape[1]),
                tf.less(y_max_anchor, im_shape[0])
            )
        )
        anchor_filter = tf.reshape(anchor_filter, [-1])
        all_anchors = tf.boolean_mask(
            all_anchors, anchor_filter, name='filter_anchors'
        )
        rpn_bbox_pred = tf.boolean_mask(rpn_bbox_pred, anchor_filter)
        all_scores = tf.boolean_mask(all_scores, anchor_filter)

    # Decode the bounding boxes.
    all_proposals = decode(all_anchors, rpn_bbox_pred)

    # Filter proposals with negative areas.
    (x_min, y_min, x_max, y_max) = tf.unstack(all_proposals, axis=1)
    zero_area_filter = tf.greater(
        tf.maximum(x_max - x_min, 0.0) * tf.maximum(y_max - y_min, 0.0),
        0.0
    )
    proposal_filter = zero_area_filter

    unsorted_scores = tf.boolean_mask(
        all_scores, proposal_filter,
        name='filtered_scores'
    )
    unsorted_proposals = tf.boolean_mask(
        all_proposals, proposal_filter,
        name='filtered_proposals'
    )

    # Get top `pre_nms_top_n` indices by sorting the proposals by score.
    pre_nms_top_n = 12000
    post_nms_top_n = 2000
    nms_threshold = 0.7

    k = tf.minimum(pre_nms_top_n, tf.shape(unsorted_scores)[0])
    top_k = tf.nn.top_k(unsorted_scores, k=k)

    sorted_top_proposals = tf.gather(unsorted_proposals, top_k.indices)
    sorted_top_scores = top_k.values

    with tf.name_scope('nms'):
        # We reorder the proposals into TensorFlows bounding box order
        # for `tf.image.non_max_supression` compatibility.
        proposals_tf_order = change_order(sorted_top_proposals)
        # We cut the pre_nms filter in pure TF version and go straight
        # into NMS.
        selected_indices = tf.image.non_max_suppression(
            proposals_tf_order, tf.reshape(
                sorted_top_scores, [-1]
            ),
            post_nms_top_n, iou_threshold=nms_threshold
        )

        # Selected_indices is a smaller tensor, we need to extract the
        # proposals and scores using it.
        nms_proposals_tf_order = tf.gather(
            proposals_tf_order, selected_indices,
            name='gather_nms_proposals'
        )

        # We switch back again to the regular bbox encoding.
        proposals = change_order(nms_proposals_tf_order)
        scores = tf.gather(
            sorted_top_scores, selected_indices,
            name='gather_nms_proposals_scores'
        )

    pred = {
        'proposals': proposals,
        'scores': scores,
    }

    return pred


def main():
    raw_image = Image.open('/home/nagitsu/images/cat.jpg')
    image = np.expand_dims(raw_image.convert('RGB'), axis=0)

    inputs = tf.placeholder(tf.float32, shape=[None, None, None, 3])
    model = build_rpn(inputs)

    init_op = tf.group(
        tf.global_variables_initializer(),
        tf.local_variables_initializer()
    )

    # TEMP: Build the mapping from Luminoth's checkpoint to the RPN's.
    to_restore = {}

    # First set up the pretrained. Prepend `truncated_base_network/`.
    pretrained_vars = tf.get_collection(
        tf.GraphKeys.GLOBAL_VARIABLES,
        scope='resnet_v1_101'
    )
    for var in pretrained_vars:
        to_restore['truncated_base_network/{}'.format(var.op.name)] = var

    # Then the convs.
    rpn_vars = tf.get_collection(
        tf.GraphKeys.GLOBAL_VARIABLES,
        scope='rpn'
    )
    for var in rpn_vars:
        var_name, weight_name = var.op.name.rsplit('/', 1)
        if weight_name == 'bias':
            to_restore['fasterrcnn/{}/b'.format(var_name)] = var
        elif weight_name == 'kernel':
            to_restore['fasterrcnn/{}/w'.format(var_name)] = var

    saver = tf.train.Saver(to_restore)

    with tf.Session() as sess:
        # sess.run(init_op)

        saver.restore(sess, '/home/nagitsu/.luminoth/checkpoints/e1c2565b51e9/model.ckpt-930305')

        result = sess.run(model, feed_dict={inputs: image})

        saver.save(sess, 'checkpoint/rpn')

    draw_bboxes(raw_image, result['proposals'][:10])
    raw_image.save('out.png')


if __name__ == '__main__':
    main()