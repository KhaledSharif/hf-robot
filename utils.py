import logging
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Callable
import datasets
import einops
import gymnasium as gym
import numpy as np
import torch
from PIL import Image as PILImage
from datasets import Dataset, Features, Image, Sequence, Value
from datasets import concatenate_datasets
from datasets.utils import disable_progress_bars, enable_progress_bars
from torch import Tensor
from tqdm import trange
from lerobot.common.datasets.utils import hf_transform_to_torch
from lerobot.common.envs.utils import preprocess_observation
from lerobot.common.logger import Logger
from lerobot.common.policies.policy_protocol import Policy
from lerobot.common.policies.policy_protocol import PolicyWithUpdate
from lerobot.common.policies.utils import get_device_from_parameters
from lerobot.common.utils.io_utils import write_video
from lerobot.common.utils.utils import (
    format_big_number,
)


def add_episodes_inplace(
        online_dataset: torch.utils.data.Dataset,
        concat_dataset: torch.utils.data.ConcatDataset,
        sampler: torch.utils.data.WeightedRandomSampler,
        hf_dataset: datasets.Dataset,
        episode_data_index: dict[str, torch.Tensor],
        pc_online_samples: float,
):
    """
    Modifies the online_dataset, concat_dataset, and sampler in place by integrating
    new episodes from hf_dataset into the online_dataset, updating the concatenated
    dataset's structure and adjusting the sampling strategy based on the specified
    percentage of online samples.

    Parameters:
    - online_dataset (torch.utils.data.Dataset): The existing online dataset to be updated.
    - concat_dataset (torch.utils.data.ConcatDataset): The concatenated dataset that combines
      offline and online datasets, used for sampling purposes.
    - sampler (torch.utils.data.WeightedRandomSampler): A sampler that will be updated to
      reflect changes in the dataset sizes and specified sampling weights.
    - hf_dataset (datasets.Dataset): A Hugging Face dataset containing the new episodes to be added.
    - episode_data_index (dict): A dictionary containing two keys ("from" and "to") associated to dataset indices.
      They indicate the start index and end index of each episode in the dataset.
    - pc_online_samples (float): The target percentage of samples that should come from
      the online dataset during sampling operations.

    Raises:
    - AssertionError: If the first episode_id or index in hf_dataset is not 0
    """
    first_episode_idx = hf_dataset.select_columns("episode_index")[0][
        "episode_index"
    ].item()
    last_episode_idx = hf_dataset.select_columns("episode_index")[-1][
        "episode_index"
    ].item()
    first_index = hf_dataset.select_columns("index")[0]["index"].item()
    last_index = hf_dataset.select_columns("index")[-1]["index"].item()
    # sanity check
    assert first_episode_idx == 0, f"{first_episode_idx=} is not 0"
    assert first_index == 0, f"{first_index=} is not 0"
    assert first_index == episode_data_index["from"][first_episode_idx].item()
    assert last_index == episode_data_index["to"][last_episode_idx].item() - 1

    if len(online_dataset) == 0:
        # initialize online dataset
        online_dataset.hf_dataset = hf_dataset
        online_dataset.episode_data_index = episode_data_index
    else:
        # get the starting indices of the new episodes and frames to be added
        start_episode_idx = last_episode_idx + 1
        start_index = last_index + 1

        def shift_indices(episode_index, index):
            # note: we don't shift "frame_index" since it represents the
            # index of the frame in the episode it belongs to
            example = {
                "episode_index": episode_index + start_episode_idx,
                "index": index + start_index,
            }
            return example

        disable_progress_bars()  # map has a tqdm progress bar
        hf_dataset = hf_dataset.map(
            shift_indices, input_columns=["episode_index", "index"]
        )
        enable_progress_bars()

        episode_data_index["from"] += start_index
        episode_data_index["to"] += start_index

        # extend online dataset
        online_dataset.hf_dataset = concatenate_datasets(
            [online_dataset.hf_dataset, hf_dataset]
        )

    # update the concatenated dataset length used during sampling
    concat_dataset.cumulative_sizes = concat_dataset.cumsum(concat_dataset.datasets)

    # update the sampling weights for each frame so that online frames get sampled a certain percentage of times
    len_online = len(online_dataset)
    len_offline = len(concat_dataset) - len_online
    weight_offline = 1.0
    weight_online = calculate_online_sample_weight(
        len_offline, len_online, pc_online_samples
    )
    sampler.weights = torch.tensor(
        [weight_offline] * len_offline + [weight_online] * len(online_dataset)
    )

    # update the total number of samples used during sampling
    sampler.num_samples = len(concat_dataset)



def rollout(
        env: gym.vector.VectorEnv,
        policy: Policy,
        seeds: list[int] | None = None,
        return_observations: bool = False,
        render_callback: Callable[[gym.vector.VectorEnv], None] | None = None,
        enable_progbar: bool = False,
) -> dict:
    """Run a batched policy rollout once through a batch of environments.

    Note that all environments in the batch are run until the last environment is done. This means some
    data will probably need to be discarded (for environments that aren't the first one to be done).

    The return dictionary contains:
        (optional) "observation": A dictionary of (batch, sequence + 1, *) tensors mapped to observation
            keys. NOTE the that this has an extra sequence element relative to the other keys in the
            dictionary. This is because an extra observation is included for after the environment is
            terminated or truncated.
        "action": A (batch, sequence, action_dim) tensor of actions applied based on the observations (not
            including the last observations).
        "reward": A (batch, sequence) tensor of rewards received for applying the actions.
        "success": A (batch, sequence) tensor of success conditions (the only time this can be True is upon
            environment termination/truncation).
        "don": A (batch, sequence) tensor of **cumulative** done conditions. For any given batch element,
            the first True is followed by True's all the way till the end. This can be used for masking
            extraneous elements from the sequences above.

    Args:
        env: The batch of environments.
        policy: The policy.
        seeds: The environments are seeded once at the start of the rollout. If provided, this argument
            specifies the seeds for each of the environments.
        return_observations: Whether to include all observations in the returned rollout data. Observations
            are returned optionally because they typically take more memory to cache. Defaults to False.
        render_callback: Optional rendering callback to be used after the environments are reset, and after
            every step.
        enable_progbar: Enable a progress bar over rollout steps.
    Returns:
        The dictionary described above.
    """
    device = get_device_from_parameters(policy)

    # Reset the policy and environments.
    policy.reset()

    observation, info = env.reset(seed=seeds)
    if render_callback is not None:
        render_callback(env)

    all_observations = []
    all_actions = []
    all_rewards = []
    all_successes = []
    all_dones = []

    step = 0
    # Keep track of which environments are done.
    done = np.array([False] * env.num_envs)
    max_steps = env.call("_max_episode_steps")[0]
    progbar = trange(
        max_steps,
        desc=f"Running rollout with at most {max_steps} steps",
        disable=not enable_progbar,
        leave=False,
    )
    while not np.all(done):
        # Numpy array to tensor and changing dictionary keys to LeRobot policy format.
        observation = preprocess_observation(observation)
        if return_observations:
            all_observations.append(deepcopy(observation))

        observation = {key: observation[key].to(device, non_blocking=True) for key in observation}

        with torch.inference_mode():
            action = policy.select_action(observation)

        # Convert to CPU / numpy.
        action = action.to("cpu").numpy()
        assert action.ndim == 2, "Action dimensions should be (batch, action_dim)"

        # Apply the next action.
        observation, reward, terminated, truncated, info = env.step(action)
        if render_callback is not None:
            render_callback(env)

        # VectorEnv stores is_success in `info["final_info"][env_index]["is_success"]`. "final_info" isn't
        # available of none of the envs finished.
        if "final_info" in info:
            successes = [info["is_success"] if info is not None else False for info in info["final_info"]]
        else:
            successes = [False] * env.num_envs

        # Keep track of which environments are done so far.
        done = terminated | truncated | done

        all_actions.append(torch.from_numpy(action))
        all_rewards.append(torch.from_numpy(reward))
        all_dones.append(torch.from_numpy(done))
        all_successes.append(torch.tensor(successes))

        step += 1
        running_success_rate = (
            einops.reduce(torch.stack(all_successes, dim=1), "b n -> b", "any").numpy().mean()
        )
        progbar.set_postfix({"running_success_rate": f"{running_success_rate.item() * 100:.1f}%"})
        progbar.update()

    # Track the final observation.
    if return_observations:
        observation = preprocess_observation(observation)
        all_observations.append(deepcopy(observation))

    # Stack the sequence along the first dimension so that we have (batch, sequence, *) tensors.
    ret = {
        "action": torch.stack(all_actions, dim=1),
        "reward": torch.stack(all_rewards, dim=1),
        "success": torch.stack(all_successes, dim=1),
        "done": torch.stack(all_dones, dim=1),
    }
    if return_observations:
        stacked_observations = {}
        for key in all_observations[0]:
            stacked_observations[key] = torch.stack([obs[key] for obs in all_observations], dim=1)
        ret["observation"] = stacked_observations

    return ret


def eval_policy(
        env: gym.vector.VectorEnv,
        policy: torch.nn.Module,
        n_episodes: int,
        max_episodes_rendered: int = 0,
        video_dir: Path | None = None,
        return_episode_data: bool = False,
        start_seed: int | None = None,
        enable_progbar: bool = False,
        enable_inner_progbar: bool = False,
) -> dict:
    """
    Args:
        env: The batch of environments.
        policy: The policy.
        n_episodes: The number of episodes to evaluate.
        max_episodes_rendered: Maximum number of episodes to render into videos.
        video_dir: Where to save rendered videos.
        return_episode_data: Whether to return episode data for online training. Incorporates the data into
            the "episodes" key of the returned dictionary.
        start_seed: The first seed to use for the first individual rollout. For all subsequent rollouts the
            seed is incremented by 1. If not provided, the environments are not manually seeded.
        enable_progbar: Enable progress bar over batches.
        enable_inner_progbar: Enable progress bar over steps in each batch.
    Returns:
        Dictionary with metrics and data regarding the rollouts.
    """
    start = time.time()
    policy.eval()

    # Determine how many batched rollouts we need to get n_episodes. Note that if n_episodes is not evenly
    # divisible by env.num_envs we end up discarding some data in the last batch.
    n_batches = n_episodes // env.num_envs + int((n_episodes % env.num_envs) != 0)

    # Keep track of some metrics.
    sum_rewards = []
    max_rewards = []
    all_successes = []
    all_seeds = []
    threads = []  # for video saving threads
    n_episodes_rendered = 0  # for saving the correct number of videos

    # Callback for visualization.
    def render_frame(env: gym.vector.VectorEnv):
        # noqa: B023
        if n_episodes_rendered >= max_episodes_rendered:
            return
        n_to_render_now = min(max_episodes_rendered - n_episodes_rendered, env.num_envs)
        if isinstance(env, gym.vector.SyncVectorEnv):
            ep_frames.append(np.stack([env.envs[i].render() for i in range(n_to_render_now)]))  # noqa: B023
        elif isinstance(env, gym.vector.AsyncVectorEnv):
            # Here we must render all frames and discard any we don't need.
            ep_frames.append(np.stack(env.call("render")[:n_to_render_now]))

    if max_episodes_rendered > 0:
        video_paths: list[str] = []

    if return_episode_data:
        episode_data: dict | None = None

    progbar = trange(n_batches, desc="Stepping through eval batches", disable=not enable_progbar)
    for batch_ix in progbar:
        # Cache frames for rendering videos. Each item will be (b, h, w, c), and the list indexes the rollout
        # step.
        if max_episodes_rendered > 0:
            ep_frames: list[np.ndarray] = []

        seeds = range(start_seed + (batch_ix * env.num_envs), start_seed + ((batch_ix + 1) * env.num_envs))
        rollout_data = rollout(
            env,
            policy,
            seeds=seeds,
            return_observations=return_episode_data,
            render_callback=render_frame if max_episodes_rendered > 0 else None,
            enable_progbar=enable_inner_progbar,
        )

        # Figure out where in each rollout sequence the first done condition was encountered (results after
        # this won't be included).
        n_steps = rollout_data["done"].shape[1]
        # Note: this relies on a property of argmax: that it returns the first occurrence as a tiebreaker.
        done_indices = torch.argmax(rollout_data["done"].to(int), axis=1)  # (batch_size, rollout_steps)
        # Make a mask with shape (batch, n_steps) to mask out rollout data after the first done
        # (batch-element-wise). Note the `done_indices + 1` to make sure to keep the data from the done step.
        mask = (torch.arange(n_steps) <= einops.repeat(done_indices + 1, "b -> b s", s=n_steps)).int()
        # Extend metrics.
        batch_sum_rewards = einops.reduce((rollout_data["reward"] * mask), "b n -> b", "sum")
        sum_rewards.extend(batch_sum_rewards.tolist())
        batch_max_rewards = einops.reduce((rollout_data["reward"] * mask), "b n -> b", "max")
        max_rewards.extend(batch_max_rewards.tolist())
        batch_successes = einops.reduce((rollout_data["success"] * mask), "b n -> b", "any")
        all_successes.extend(batch_successes.tolist())
        all_seeds.extend(seeds)

        if return_episode_data:
            this_episode_data = _compile_episode_data(
                rollout_data,
                done_indices,
                start_episode_index=batch_ix * env.num_envs,
                start_data_index=(
                    0 if episode_data is None else (episode_data["episode_data_index"]["to"][-1].item())
                ),
                fps=env.unwrapped.metadata["render_fps"],
            )
            if episode_data is None:
                episode_data = this_episode_data
            else:
                # Some sanity checks to make sure we are not correctly compiling the data.
                assert (
                        episode_data["hf_dataset"]["episode_index"][-1] + 1
                        == this_episode_data["hf_dataset"]["episode_index"][0]
                )
                assert (
                        episode_data["hf_dataset"]["index"][-1] + 1 == this_episode_data["hf_dataset"]["index"][0]
                )
                assert torch.equal(
                    episode_data["episode_data_index"]["to"][-1],
                    this_episode_data["episode_data_index"]["from"][0],
                )
                # Concatenate the episode data.
                episode_data = {
                    "hf_dataset": concatenate_datasets(
                        [episode_data["hf_dataset"], this_episode_data["hf_dataset"]]
                    ),
                    "episode_data_index": {
                        k: torch.cat(
                            [
                                episode_data["episode_data_index"][k],
                                this_episode_data["episode_data_index"][k],
                            ]
                        )
                        for k in ["from", "to"]
                    },
                }

        # Maybe render video for visualization.
        if max_episodes_rendered > 0 and len(ep_frames) > 0:
            batch_stacked_frames = np.stack(ep_frames, axis=1)  # (b, t, *)
            for stacked_frames, done_index in zip(
                    batch_stacked_frames, done_indices.flatten().tolist(), strict=False
            ):
                if n_episodes_rendered >= max_episodes_rendered:
                    break
                video_dir.mkdir(parents=True, exist_ok=True)
                video_path = video_dir / f"eval_episode_{n_episodes_rendered}.mp4"
                video_paths.append(str(video_path))
                thread = threading.Thread(
                    target=write_video,
                    args=(
                        str(video_path),
                        stacked_frames[: done_index + 1],  # + 1 to capture the last observation
                        env.unwrapped.metadata["render_fps"],
                    ),
                )
                thread.start()
                threads.append(thread)
                n_episodes_rendered += 1

        progbar.set_postfix(
            {"running_success_rate": f"{np.mean(all_successes[:n_episodes]).item() * 100:.1f}%"}
        )

    # Wait till all video rendering threads are done.
    for thread in threads:
        thread.join()

    # Compile eval info.
    info = {
        "per_episode": [
            {
                "episode_ix": i,
                "sum_reward": sum_reward,
                "max_reward": max_reward,
                "success": success,
                "seed": seed,
            }
            for i, (sum_reward, max_reward, success, seed) in enumerate(
                zip(
                    sum_rewards[:n_episodes],
                    max_rewards[:n_episodes],
                    all_successes[:n_episodes],
                    all_seeds[:n_episodes],
                    strict=True,
                )
            )
        ],
        "aggregated": {
            "avg_sum_reward": float(np.nanmean(sum_rewards[:n_episodes])),
            "avg_max_reward": float(np.nanmean(max_rewards[:n_episodes])),
            "pc_success": float(np.nanmean(all_successes[:n_episodes]) * 100),
            "eval_s": time.time() - start,
            "eval_ep_s": (time.time() - start) / n_episodes,
        },
    }

    if return_episode_data:
        info["episodes"] = episode_data

    if max_episodes_rendered > 0:
        info["video_paths"] = video_paths

    return info


def _compile_episode_data(
        rollout_data: dict, done_indices: Tensor, start_episode_index: int, start_data_index: int, fps: float
) -> dict:
    """Convenience function for `eval_policy(return_episode_data=True)`

    Compiles all the rollout data into a Hugging Face dataset.

    Similar logic is implemented when datasets are pushed to hub (see: `push_to_hub`).
    """
    ep_dicts = []
    episode_data_index = {"from": [], "to": []}
    total_frames = 0
    data_index_from = start_data_index
    for ep_ix in range(rollout_data["action"].shape[0]):
        num_frames = done_indices[ep_ix].item() + 1  # + 1 to include the first done frame
        total_frames += num_frames

        # TODO(rcadene): We need to add a missing last frame which is the observation
        # of a done state. it is critical to have this frame for tdmpc to predict a "done observation/state"
        ep_dict = {
            "action": rollout_data["action"][ep_ix, :num_frames],
            "episode_index": torch.tensor([start_episode_index + ep_ix] * num_frames),
            "frame_index": torch.arange(0, num_frames, 1),
            "timestamp": torch.arange(0, num_frames, 1) / fps,
            "next.done": rollout_data["done"][ep_ix, :num_frames],
            "next.reward": rollout_data["reward"][ep_ix, :num_frames].type(torch.float32),
        }
        for key in rollout_data["observation"]:
            ep_dict[key] = rollout_data["observation"][key][ep_ix][:num_frames]
        ep_dicts.append(ep_dict)

        episode_data_index["from"].append(data_index_from)
        episode_data_index["to"].append(data_index_from + num_frames)

        data_index_from += num_frames

    data_dict = {}
    for key in ep_dicts[0]:
        if "image" not in key:
            data_dict[key] = torch.cat([x[key] for x in ep_dicts])
        else:
            if key not in data_dict:
                data_dict[key] = []
            for ep_dict in ep_dicts:
                for img in ep_dict[key]:
                    # sanity check that images are channel first
                    c, h, w = img.shape
                    assert c < h and c < w, f"expect channel first images, but instead {img.shape}"

                    # sanity check that images are float32 in range [0,1]
                    assert img.dtype == torch.float32, f"expect torch.float32, but instead {img.dtype=}"
                    assert img.max() <= 1, f"expect pixels lower than 1, but instead {img.max()=}"
                    assert img.min() >= 0, f"expect pixels greater than 1, but instead {img.min()=}"

                    # from float32 in range [0,1] to uint8 in range [0,255]
                    img *= 255
                    img = img.type(torch.uint8)

                    # convert to channel last and numpy as expected by PIL
                    img = PILImage.fromarray(img.permute(1, 2, 0).numpy())

                    data_dict[key].append(img)

    data_dict["index"] = torch.arange(start_data_index, start_data_index + total_frames, 1)
    episode_data_index["from"] = torch.tensor(episode_data_index["from"])
    episode_data_index["to"] = torch.tensor(episode_data_index["to"])

    # TODO(rcadene): clean this
    features = {}
    for key in rollout_data["observation"]:
        if "image" in key:
            features[key] = Image()
        else:
            features[key] = Sequence(length=data_dict[key].shape[1], feature=Value(dtype="float32", id=None))
    features.update(
        {
            "action": Sequence(length=data_dict["action"].shape[1], feature=Value(dtype="float32", id=None)),
            "episode_index": Value(dtype="int64", id=None),
            "frame_index": Value(dtype="int64", id=None),
            "timestamp": Value(dtype="float32", id=None),
            "next.reward": Value(dtype="float32", id=None),
            "next.done": Value(dtype="bool", id=None),
            # 'next.success': Value(dtype='bool', id=None),
            "index": Value(dtype="int64", id=None),
        }
    )
    features = Features(features)
    hf_dataset = Dataset.from_dict(data_dict, features=features)
    hf_dataset.set_transform(hf_transform_to_torch)
    return {
        "hf_dataset": hf_dataset,
        "episode_data_index": episode_data_index,
    }


def make_optimizer_and_scheduler(cfg, policy):
    if cfg.policy.name == "act":
        optimizer_params_dicts = [
            {
                "params": [
                    p
                    for n, p in policy.named_parameters()
                    if not n.startswith("backbone") and p.requires_grad
                ]
            },
            {
                "params": [
                    p
                    for n, p in policy.named_parameters()
                    if n.startswith("backbone") and p.requires_grad
                ],
                "lr": cfg.training.lr_backbone,
            },
        ]
        optimizer = torch.optim.AdamW(
            optimizer_params_dicts,
            lr=cfg.training.lr,
            weight_decay=cfg.training.weight_decay,
        )
        lr_scheduler = None
    elif cfg.policy.name == "diffusion":
        optimizer = torch.optim.Adam(
            policy.diffusion.parameters(),
            cfg.training.lr,
            cfg.training.adam_betas,
            cfg.training.adam_eps,
            cfg.training.adam_weight_decay,
        )
        assert (
                cfg.training.online_steps == 0
        ), "Diffusion Policy does not handle online training."
        from diffusers.optimization import get_scheduler

        lr_scheduler = get_scheduler(
            cfg.training.lr_scheduler,
            optimizer=optimizer,
            num_warmup_steps=cfg.training.lr_warmup_steps,
            num_training_steps=cfg.training.offline_steps,
        )
    elif policy.name == "tdmpc":
        optimizer = torch.optim.Adam(policy.parameters(), cfg.training.lr)
        lr_scheduler = None
    else:
        raise NotImplementedError()

    return optimizer, lr_scheduler


def update_policy(policy, batch, optimizer, grad_clip_norm, lr_scheduler=None):
    """Returns a dictionary of items for logging."""
    start_time = time.time()
    policy.train()
    output_dict = policy.forward(batch)
    # TODO(rcadene): policy.unnormalize_outputs(out_dict)
    loss = output_dict["loss"]
    loss.backward()
    grad_norm = torch.nn.utils.clip_grad_norm_(
        policy.parameters(),
        grad_clip_norm,
        error_if_nonfinite=False,
    )

    optimizer.step()
    optimizer.zero_grad()

    if lr_scheduler is not None:
        lr_scheduler.step()

    if isinstance(policy, PolicyWithUpdate):
        # To possibly update an internal buffer (for instance an Exponential Moving Average like in TDMPC).
        policy.update()

    info = {
        "loss": loss.item(),
        "grad_norm": float(grad_norm),
        "lr": optimizer.param_groups[0]["lr"],
        "update_s": time.time() - start_time,
        **{k: v for k, v in output_dict.items() if k != "loss"},
    }

    return info


def log_train_info(logger: Logger, info, step, cfg, dataset, is_offline):
    loss = info["loss"]
    grad_norm = info["grad_norm"]
    lr = info["lr"]
    update_s = info["update_s"]

    # A sample is an (observation,action) pair, where observation and action
    # can be on multiple timestamps. In a batch, we have `batch_size`` number of samples.
    num_samples = (step + 1) * cfg.training.batch_size
    avg_samples_per_ep = dataset.num_samples / dataset.num_episodes
    num_episodes = num_samples / avg_samples_per_ep
    num_epochs = num_samples / dataset.num_samples
    log_items = [
        f"step:{format_big_number(step)}",
        # number of samples seen during training
        f"smpl:{format_big_number(num_samples)}",
        # number of episodes seen during training
        f"ep:{format_big_number(num_episodes)}",
        # number of time all unique samples are seen
        f"epch:{num_epochs:.2f}",
        f"loss:{loss:.3f}",
        f"grdn:{grad_norm:.3f}",
        f"lr:{lr:0.1e}",
        # in seconds
        f"updt_s:{update_s:.3f}",
    ]
    logging.info(" ".join(log_items))

    info["step"] = step
    info["num_samples"] = num_samples
    info["num_episodes"] = num_episodes
    info["num_epochs"] = num_epochs
    info["is_offline"] = is_offline

    logger.log_dict(info, step, mode="train")


def log_eval_info(logger, info, step, cfg, dataset, is_offline):
    eval_s = info["eval_s"]
    avg_sum_reward = info["avg_sum_reward"]
    pc_success = info["pc_success"]

    # A sample is an (observation,action) pair, where observation and action
    # can be on multiple timestamps. In a batch, we have `batch_size`` number of samples.
    num_samples = (step + 1) * cfg.training.batch_size
    avg_samples_per_ep = dataset.num_samples / dataset.num_episodes
    num_episodes = num_samples / avg_samples_per_ep
    num_epochs = num_samples / dataset.num_samples
    log_items = [
        f"step:{format_big_number(step)}",
        # number of samples seen during training
        f"smpl:{format_big_number(num_samples)}",
        # number of episodes seen during training
        f"ep:{format_big_number(num_episodes)}",
        # number of time all unique samples are seen
        f"epch:{num_epochs:.2f}",
        f"∑rwrd:{avg_sum_reward:.3f}",
        f"success:{pc_success:.1f}%",
        f"eval_s:{eval_s:.3f}",
    ]
    logging.info(" ".join(log_items))

    info["step"] = step
    info["num_samples"] = num_samples
    info["num_episodes"] = num_episodes
    info["num_epochs"] = num_epochs
    info["is_offline"] = is_offline

    logger.log_dict(info, step, mode="eval")


def calculate_online_sample_weight(n_off: int, n_on: int, pc_on: float):
    """
    Calculate the sampling weight to be assigned to samples so that a specified percentage of the batch comes from online dataset (on average).

    Parameters:
    - n_off (int): Number of offline samples, each with a sampling weight of 1.
    - n_on (int): Number of online samples.
    - pc_on (float): Desired percentage of online samples in decimal form (e.g., 50% as 0.5).

    The total weight of offline samples is n_off * 1.0.
    The total weight of offline samples is n_on * w.
    The total combined weight of all samples is n_off + n_on * w.
    The fraction of the weight that is online is n_on * w / (n_off + n_on * w).
    We want this fraction to equal pc_on, so we set up the equation n_on * w / (n_off + n_on * w) = pc_on.
    The solution is w = - (n_off * pc_on) / (n_on * (pc_on - 1))
    """
    assert 0.0 <= pc_on <= 1.0
    return -(n_off * pc_on) / (n_on * (pc_on - 1))
