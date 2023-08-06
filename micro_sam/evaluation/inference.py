import os
import pickle

from copy import deepcopy
from typing import Any, Dict, List, Optional, Union

import imageio.v3 as imageio
import numpy as np
import torch

from skimage.segmentation import relabel_sequential
from tqdm import tqdm

from segment_anything import SamPredictor
from segment_anything.utils.transforms import ResizeLongestSide

from .. import util as util
from ..prompt_generators import PointAndBoxPromptGenerator


def _load_prompts(
    cached_point_prompts, save_point_prompts,
    cached_box_prompts, save_box_prompts,
    image_name
):

    def load_prompt_type(cached_prompts, save_prompts):
        # Check if we have saved prompts.
        if cached_prompts is None or save_prompts:  # we don't have cached prompts
            prompts = None
        elif isinstance(cached_prompts, str):  # we have cached prompts, but they have not been loaded yet
            with open(cached_prompts, "rb") as f:
                cached_prompts = pickle.load(f)
            prompts = cached_prompts[image_name]
        else:  # we have cached prompts
            prompts = cached_prompts[image_name]
        return cached_prompts, prompts

    cached_point_prompts, point_prompts = load_prompt_type(cached_point_prompts, save_point_prompts)
    cached_box_prompts, box_prompts = load_prompt_type(cached_box_prompts, save_box_prompts)

    if point_prompts is None:
        input_point, input_label = [], []
    else:
        input_point, input_label = point_prompts

    if box_prompts is None:
        input_box = []
    else:
        input_box = box_prompts

    prompts = (input_point, input_label, input_box)
    return prompts, cached_point_prompts, cached_box_prompts


def _get_batched_prompts(
    gt,
    gt_ids,
    use_points,
    use_boxes,
    n_positives,
    n_negatives,
    dilation,
    transform_function,
):
    input_point, input_label, input_box = [], [], []

    # Initialize the prompt generator.
    center_coordinates, bbox_coordinates = util.get_centers_and_bounding_boxes(gt)
    prompt_generator = PointAndBoxPromptGenerator(
        n_positive_points=n_positives, n_negative_points=n_negatives,
        dilation_strength=dilation, get_point_prompts=use_points,
        get_box_prompts=use_boxes
    )

    # Iterate over the gt ids, generate the corresponding prompts and combine them to batched input.
    for gt_id in gt_ids:
        centers, bboxes = center_coordinates.get(gt_id), bbox_coordinates.get(gt_id)
        input_point_list, input_label_list, input_box_list, objm = prompt_generator(gt, gt_id, bboxes, centers)

        if use_boxes:
            # indexes hard-coded to adapt with SAM's bbox format
            # default format: [a, b, c, d] -> SAM's format: [b, a, d, c]
            _ib = [input_box_list[0][1], input_box_list[0][0],
                   input_box_list[0][3], input_box_list[0][2]]
            # transform boxes to the expected format - see predictor.predict function for details
            _ib = transform_function.apply_boxes(np.array(_ib), gt.shape)
            input_box.append(_ib)

        if use_points:
            assert len(input_point_list) == (n_positives + n_negatives)
            _ip = [ip[::-1] for ip in input_point_list]  # to match the coordinate system used by SAM

            # transform coords to the expected format - see predictor.predict function for details
            _ip = transform_function.apply_coords(np.array(_ip), gt.shape)
            input_point.append(_ip)
            input_label.append(input_label_list)

    return input_point, input_label, input_box


def _run_inference_with_prompts_for_image(
    predictor,
    gt,
    use_points,
    use_boxes,
    n_positives,
    n_negatives,
    dilation,
    batch_size,
    cached_prompts,
):
    # We need the resize transformation for the expected model input size.
    transform_function = ResizeLongestSide(1024)
    gt_ids = np.unique(gt)[1:]

    if cached_prompts is None:
        input_point, input_label, input_box = _get_batched_prompts(
            gt, gt_ids, use_points, use_boxes, n_positives, n_negatives, dilation, transform_function,
        )
    else:
        input_point, input_label, input_box = cached_prompts

    # Make a copy of the point prompts to return them at the end.
    prompts = deepcopy((input_point, input_label, input_box))

    # Transform the prompts into batches
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    input_point = torch.tensor(np.array(input_point)).to(device) if len(input_point) > 0 else None
    input_label = torch.tensor(np.array(input_label)).to(device) if len(input_label) > 0 else None
    input_box = torch.tensor(np.array(input_box)).to(device) if len(input_box) > 0 else None

    # Use multi-masking only if we have a single positive point without box
    multimasking = False
    if not use_boxes and (n_positives == 1 and n_negatives == 0):
        multimasking = True

    # Run the batched inference.
    n_samples = input_box.shape[0] if input_point is None else input_point.shape[0]
    n_batches = int(np.ceil(float(n_samples) / batch_size))
    masks, ious = [], []
    with torch.no_grad():
        for batch_idx in range(n_batches):
            batch_start = batch_idx * batch_size
            batch_stop = min((batch_idx + 1) * batch_size, n_samples)

            batch_points = None if input_point is None else input_point[batch_start:batch_stop]
            batch_labels = None if input_label is None else input_label[batch_start:batch_stop]
            batch_boxes = None if input_box is None else input_box[batch_start:batch_stop]

            batch_masks, batch_ious, _ = predictor.predict_torch(
                point_coords=batch_points, point_labels=batch_labels,
                boxes=batch_boxes, multimask_output=multimasking
            )
            masks.append(batch_masks)
            ious.append(batch_ious)
    masks = torch.cat(masks)
    ious = torch.cat(ious)
    assert len(masks) == len(ious) == n_samples

    # TODO we should actually use non-max suppression here
    # I will implement it somewhere to have it refactored
    instance_labels = np.zeros_like(gt, dtype=int)
    for m, iou, gt_idx in zip(masks, ious, gt_ids):
        best_idx = torch.argmax(iou)
        best_mask = m[best_idx]
        instance_labels[best_mask.detach().cpu().numpy()] = gt_idx

    return instance_labels, prompts


def get_predictor(checkpoint_path, model_type):
    """@private"""
    # TODO use try-except rather than this construct, so that we don't rely on the checkpoint name
    if checkpoint_path.split("/")[-1] == "best.pt":  # Finetuned SAM model
        predictor = util.get_custom_sam_model(checkpoint_path=checkpoint_path, model_type=model_type)
    else:  # Vanilla SAM model
        predictor = util.get_sam_model(model_type=model_type, checkpoint_path=checkpoint_path)  # type: ignore
    return predictor


def precompute_all_embeddings(
    predictor: SamPredictor,
    image_paths: List[Union[str, os.PathLike]],
    embedding_dir: Union[str, os.PathLike],
):
    """Precompute all image embeddings.

    To enable running different inference tasks in parallel afterwards.

    Args:
        predictor: The SegmentAnything predictor.
        image_paths: The image file paths.
        embedding_dir: The directory where the embeddings will be saved.
    """
    for image_path in tqdm(image_paths, desc="Precompute embeddings"):
        image_name = os.path.basename(image_path)
        im = imageio.imread(image_path)
        embedding_path = os.path.join(embedding_dir, f"{image_name[:-4]}.zarr")
        util.precompute_image_embeddings(predictor, im, embedding_path)


def _precompute_prompts(gt_path, use_points, use_boxes, n_positives, n_negatives, dilation, transform_function):
    name = os.path.basename(gt_path)

    gt = imageio.imread(gt_path).astype("uint32")
    gt = relabel_sequential(gt)[0]
    gt_ids = np.unique(gt)[1:]

    input_point, input_label, input_box = _get_batched_prompts(
        gt, gt_ids, use_points, use_boxes, n_positives, n_negatives, dilation, transform_function
    )

    if use_boxes and not use_points:
        return name, input_box
    return name, (input_point, input_label)


def precompute_all_prompts(
    gt_paths: List[Union[str, os.PathLike]],
    prompt_save_dir: Union[str, os.PathLike],
    prompt_settings: List[Dict[str, Any]],
) -> None:
    """Precompute all point prompts.

    To enable running different inference tasks in parallel afterwards.

    Args:
        gt_paths: The file paths to the ground-truth segmentations.
        prompt_save_dir: The directory where the prompt files will be saved.
        prompt_settings: The settings for which the prompts will be computed.
    """
    os.makedirs(prompt_save_dir, exist_ok=True)
    transform_function = ResizeLongestSide(1024)

    for settings in tqdm(prompt_settings, desc="Precompute prompts"):

        use_points, use_boxes = settings["use_points"], settings["use_boxes"]
        n_positives, n_negatives = settings["n_positives"], settings["n_negatives"]
        dilation = settings.get("dilation", 5)

        # check if the prompts were already computed
        if use_boxes and not use_points:
            prompt_save_path = os.path.join(prompt_save_dir, "boxes.pkl")
        else:
            prompt_save_path = os.path.join(prompt_save_dir, f"points-p{n_positives}-n{n_negatives}.pkl")
        if os.path.exists(prompt_save_path):
            continue

        results = []
        for gt_path in tqdm(gt_paths, desc=f"Precompute prompts for p{n_positives}-n{n_negatives}"):
            prompts = _precompute_prompts(
                gt_path,
                use_points=use_points,
                use_boxes=use_boxes,
                n_positives=n_positives,
                n_negatives=n_negatives,
                dilation=dilation,
                transform_function=transform_function,
            )
            results.append(prompts)

        saved_prompts = {res[0]: res[1] for res in results}
        with open(prompt_save_path, "wb") as f:
            pickle.dump(saved_prompts, f)


def _get_prompt_caching(prompt_save_dir, use_points, use_boxes, n_positives, n_negatives):

    def get_prompt_type_caching(use_type, save_name):
        if not use_type:
            return None, False, None

        prompt_save_path = os.path.join(prompt_save_dir, save_name)
        if os.path.exists(prompt_save_path):
            print("Using precomputed prompts from", prompt_save_path)
            # We delay loading the prompts, so we only have to load them once they're needed the first time.
            # This avoids loading the prompts (which are in a big pickle file) if all predictions are done already.
            cached_prompts = prompt_save_path
            save_prompts = False
        else:
            print("Saving prompts in", prompt_save_path)
            cached_prompts = {}
            save_prompts = True
        return cached_prompts, save_prompts, prompt_save_path

    # Check if prompt serialization is enabled.
    # If it is then load the prompts if they are already cached and otherwise store them.
    if prompt_save_dir is None:
        print("Prompts are not cached.")
        cached_point_prompts, cached_box_prompts = None, None
        save_point_prompts, save_box_prompts = False, False
        point_prompt_save_path, box_prompt_save_path = None, None
    else:
        cached_point_prompts, save_point_prompts, point_prompt_save_path = get_prompt_type_caching(
            use_points, f"points-p{n_positives}-n{n_negatives}.pkl"
        )
        cached_box_prompts, save_box_prompts, box_prompt_save_path = get_prompt_type_caching(
            use_boxes, "boxes.pkl"
        )

    return (cached_point_prompts, save_point_prompts, point_prompt_save_path,
            cached_box_prompts, save_box_prompts, box_prompt_save_path)


def run_inference_with_prompts(
    predictor: SamPredictor,
    image_paths: List[Union[str, os.PathLike]],
    gt_paths: List[Union[str, os.PathLike]],
    embedding_dir: Union[str, os.PathLike],
    prediction_dir: Union[str, os.PathLike],
    use_points: bool,
    use_boxes: bool,
    n_positives: int,
    n_negatives: int,
    dilation: int = 5,
    prompt_save_dir: Optional[Union[str, os.PathLike]] = None,
    batch_size: int = 512,
) -> None:
    """Run segment anything inference for multiple images using prompts derived form groundtruth.

    Args:
        predictor: The SegmentAnything predictor.
        image_paths: The image file paths.
        gt_paths: The ground-truth segmentation file paths.
        embedding_dir: The directory where the image embddings will be saved or are already saved.
        use_points: Whether to use point prompts.
        use_boxes: Whetehr to use box prompts
        n_positives: The number of positive point prompts that will be sampled.
        n_negativess: The number of negative point prompts that will be sampled.
        dilation: The dilation factor for the radius around the ground-truth object
            around which points will not be sampled.
        prompt_save_dir: The directory where point prompts will be saved or are already saved.
            This enables running multiple experiments in a reproducible manner.
        batch_size: The batch size used for batched prediction.
    """
    if not (use_points or use_boxes):
        raise ValueError("You need to use at least one of point or box prompts.")

    if len(image_paths) != len(gt_paths):
        raise ValueError(f"Expect same number of images and gt images, got {len(image_paths)}, {len(gt_paths)}")

    (cached_point_prompts, save_point_prompts, point_prompt_save_path,
     cached_box_prompts, save_box_prompts, box_prompt_save_path) = _get_prompt_caching(
         prompt_save_dir, use_points, use_boxes, n_positives, n_negatives
     )

    for image_path, gt_path in tqdm(
        zip(image_paths, gt_paths), total=len(image_paths), desc="Run inference with prompts"
    ):
        image_name = os.path.basename(image_path)

        # We skip the images that already have been segmented.
        prediction_path = os.path.join(prediction_dir, image_name)
        if os.path.exists(prediction_path):
            continue

        assert os.path.exists(image_path), image_path
        assert os.path.exists(gt_path), gt_path

        im = imageio.imread(image_path)
        gt = imageio.imread(gt_path)
        gt = relabel_sequential(gt)[0]

        embedding_path = os.path.join(embedding_dir, f"{image_name[:-4]}.zarr")
        image_embeddings = util.precompute_image_embeddings(predictor, im, embedding_path)
        util.set_precomputed(predictor, image_embeddings)

        this_prompts, cached_point_prompts, cached_box_prompts = _load_prompts(
            cached_point_prompts, save_point_prompts,
            cached_box_prompts, save_box_prompts,
            image_name
        )
        instances, this_prompts = _run_inference_with_prompts_for_image(
            predictor, gt, n_positives=n_positives, n_negatives=n_negatives,
            dilation=dilation, use_points=use_points, use_boxes=use_boxes,
            batch_size=batch_size, cached_prompts=this_prompts
        )

        if save_point_prompts:
            cached_point_prompts[image_name] = this_prompts[:2]
        if save_box_prompts:
            cached_box_prompts[image_name] = this_prompts[-1]

        # It's important to compress here, otherwise the predictions would take up a lot of space.
        imageio.imwrite(prediction_path, instances, compression=5)

    # Save the prompts if we run experiments with prompt caching and have computed them
    # for the first time.
    if save_point_prompts:
        with open(point_prompt_save_path, "wb") as f:
            pickle.dump(cached_point_prompts, f)
    if save_box_prompts:
        with open(box_prompt_save_path, "wb") as f:
            pickle.dump(cached_box_prompts, f)