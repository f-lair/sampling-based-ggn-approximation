from typing import Any, List, Tuple

import jax
import optax
from flax.core.frozen_dict import FrozenDict
from flax.training.train_state import TrainState
from jax import numpy as jnp
from jax import tree_util
from tqdm import tqdm

from data_utils import DataLoader
from log_utils import save_results


@jax.jit
def train_step(
    state: TrainState, batch: Tuple[jax.Array, jax.Array]
) -> Tuple[TrainState, jax.Array, jax.Array]:
    """
    Performs a single training step without GGN computation.
    C: Model output dim.
    D: Parameter dim.
    N: Batch dim.

    Args:
        state (TrainState): Current training state.
        batch (Tuple[jax.Array, jax.Array]): Batched input data.

    Returns:
        Tuple[TrainState, jax.Array, jax.Array]:
            Updated training state,
            per-item loss ([N]),
            number of correct predictions ([1]).
    """

    def model_loss_fn(
        params: FrozenDict[str, Any], x: jax.Array, y: jax.Array
    ) -> Tuple[jax.Array, Tuple[jax.Array, jax.Array]]:
        """
        Performs model forward pass and evaluates mean loss as a function of model parameters.

        Args:
            params (FrozenDict[str, Any]): Model parameters ([D], pytree in D).
            x (jax.Array): Model input ([N, ...]).
            y (jax.Array): Ground truth, integer-encoded ([N]).

        Returns:
            Tuple[jax.Array, Tuple[jax.Array, jax.Array]]:
                Mean loss ([1]),
                per-item loss ([N]),
                model output ([N, C]).
        """

        logits = state.apply_fn(params, x)  # [N, C]
        loss_unreduced = optax.softmax_cross_entropy_with_integer_labels(logits, y)  # [N]
        loss = jnp.mean(loss_unreduced)  # [1]
        return loss, (loss_unreduced, logits)  # type: ignore

    # Retrieve data
    x, y = batch

    # Forward pass + gradient
    (_, (loss, logits)), d_loss = jax.value_and_grad(model_loss_fn, has_aux=True)(
        state.params, x, y
    )  # [N]; [N, C]; [D], pytree in D
    n_correct = jnp.sum(jnp.argmax(logits, -1) == y)  # [1]

    # Perform gradient step, update training state
    state = state.apply_gradients(grads=d_loss)

    return state, loss, n_correct


@jax.jit
def test_step(
    state: TrainState, batch: Tuple[jax.Array, jax.Array]
) -> Tuple[jax.Array, jax.Array]:
    """
    Performs a single test step without GGN computation.
    C: Model output dim.
    D: Parameter dim.
    N: Batch dim.

    Args:
        state (TrainState): Current training state.
        batch (Tuple[jax.Array, jax.Array]): Batched input data.

    Returns:
        Tuple[jax.Array, jax.Array]:
            Per-item loss ([N]),
            number of correct predictions ([1]).
    """

    def model_loss_fn(
        params: FrozenDict[str, Any], x: jax.Array, y: jax.Array
    ) -> Tuple[jax.Array, Tuple[jax.Array, jax.Array]]:
        """
        Performs model forward pass and evaluates mean loss as a function of model parameters.

        Args:
            params (FrozenDict[str, Any]): Model parameters ([D], pytree in D).
            x (jax.Array): Model input ([N, ...]).
            y (jax.Array): Ground truth, integer-encoded ([N]).

        Returns:
            Tuple[jax.Array, Tuple[jax.Array, jax.Array]]:
                Mean loss ([1]),
                per-item loss ([N]),
                model output ([N, C]).
        """

        logits = state.apply_fn(params, x)  # [N, C]
        loss_unreduced = optax.softmax_cross_entropy_with_integer_labels(logits, y)  # [N]
        loss = jnp.mean(loss_unreduced)  # [1]
        return loss, (loss_unreduced, logits)  # type: ignore

    # Retrieve data
    x, y = batch

    # Forward pass
    _, (loss, logits) = model_loss_fn(state.params, x, y)  # [N]; [N, C]
    n_correct = jnp.sum(jnp.argmax(logits, -1) == y)  # [1]

    return loss, n_correct


@jax.jit
def compute_ggn(state: TrainState, batch: Tuple[jax.Array, jax.Array]) -> jax.Array:
    """
    Performs a single training step with GGN computation.
    C: Model output dim.
    D: Parameter dim.
    N: Batch dim.

    Args:
        state (TrainState): Current training state.
        batch (Tuple[jax.Array, jax.Array]): Batched input data.

    Returns:
        jax.Array: Per-item GGN ([N, D, D]).
    """

    def model_fn(params: FrozenDict[str, Any], x: jax.Array) -> jax.Array:
        """
        Performs model forward pass as a function of model parameters.

        Args:
            params (FrozenDict[str, Any]): Model parameters ([D], pytree in D).
            x (jax.Array): Model input ([N, ...]).

        Returns:
            jax.Array: Model output ([N, C]).
        """

        logits = state.apply_fn(params, x)  # [N, C]
        return logits

    def loss_fn(logits: jax.Array, y: jax.Array) -> jax.Array:
        """
        Computes per-item loss as a function of model output.

        Args:
            logits (jax.Array): Model output ([N, C]).
            y (jax.Array): Ground truth, integer-encoded ([N]).

        Returns:
            jax.Array: Per-item loss ([N]).
        """

        loss = optax.softmax_cross_entropy_with_integer_labels(logits, y)  # [N]
        return loss  # type: ignore

    # Retrieve data
    x, y = batch

    # Forward pass
    logits = model_fn(state.params, x)  # [N, C]
    N, C = logits.shape

    # Define differentiating functions:
    #   - 'J_model_fn': Jacobian of model output w.r.t. model parameters
    #   - 'd_loss_fn': Gradient of loss w.r.t. model output
    #   - 'H_loss_fn': Hessian of loss w.r.t. model output
    J_model_fn = jax.jacrev(model_fn)  # [D]->[C, D], D>>C
    d_loss_fn = jax.grad(loss_fn)  # [C]->[C]
    H_loss_fn = jax.jacfwd(d_loss_fn)  # [C]->[C, C]

    # Compute differential quantities:
    #   - 'J_model': Per-item Jacobian of model output w.r.t. model parameters
    #   - 'H_loss': Per-item Hessian of loss w.r.t. model output
    J_model = J_model_fn(state.params, x)  # [N, C, D], pytree in D
    H_loss = jax.vmap(H_loss_fn)(logits, y)  # [N, C, C]

    # Transform 'J_model' from pytree representation into vector representation
    J_model = jnp.concatenate(
        [x.reshape(N, C, -1) for x in tree_util.tree_leaves(J_model)], axis=2
    )  # [N, C, D]

    # Compute per-item Generalized Gauss-Newton (GGN) matrix: J_model.T @ H_loss @ J_model
    GGN = jnp.einsum("nax,nab,nby->nxy", J_model, H_loss, J_model)  # [N, D, D]

    return GGN


def train_epoch(
    state: TrainState,
    train_dataloader: DataLoader,
    ggn_dataloader: DataLoader,
    ggn_batch_sizes: List[int],
    ggn_freq: int,
    n_ggn_iterations: int,
    n_steps: int,
    no_progress_bar: bool,
    results_path: str,
) -> Tuple[TrainState, float, float, int, int]:
    """
    Performs a single training epoch.

    Args:
        state (TrainState): Current training state.
        train_dataloader (DataLoader): Data loader for model training.
        ggn_dataloader (DataLoader): Data loader for GGN computation.
        ggn_batch_sizes (List[int]): Batch sizes for which GGNs will be saved.
        ggn_freq (int): Frequency of GGN iterations.
        n_ggn_iterations (int): Number of GGN iterations.
        n_steps (int): Current number of completed training step across epochs.
        no_progress_bar (bool): Disables progress bar.
        results_path (str): Results path.

    Returns:
        Tuple[TrainState, float, float, int, int]:
            Updated training state,
            epoch loss,
            epoch accuracy,
            current number of completed training step across epochs,
            number of remaining ggn iterations after this epoch.
    """

    # Running statistics
    loss_epoch = []  # Per-item losses per training steps
    n_correct_epoch = 0  # Number of correct predictions across the epoch
    GGN_iter_counter = 0  # Number of already completed GGN iterations

    # Start epoch
    pbar_stats = {"loss": 0.0, "acc": 0.0}
    with tqdm(
        total=len(train_dataloader), desc="Step", disable=no_progress_bar, postfix=pbar_stats
    ) as pbar:
        # Iterate over dataset for training
        for batch in train_dataloader:
            # Compute GGN, if n_ggn_iterations not reached and current step is multiple of ggn_freq
            if GGN_iter_counter < n_ggn_iterations and n_steps % ggn_freq == 0:
                # Init running statistics for this GGN iteration
                GGN_counter = 0  # Number of already computed per-item GGNs (for running average)
                GGN_total = None  # Total GGN, encompassing all per-item GGNs across the dataset
                GGN_samples = None  # GGN samples, encompassing GGNs over one/multiple data batches

                # Iterate over dataset for GGN computation
                for ggn_step_idx, ggn_batch in enumerate(
                    tqdm(ggn_dataloader, desc="GGN-sample-step", disable=no_progress_bar)
                ):
                    # Compute batch GGNs
                    GGN = compute_ggn(state, ggn_batch)  # [N, D, D]

                    # Aggregate GGN samples as running average to save memory
                    aggregated_batch_size = ggn_step_idx + 1
                    if GGN_samples is None:
                        GGN_samples = GGN.copy()  # [N, D, D]
                    else:
                        GGN_samples = GGN_samples + (GGN - GGN_samples) / (
                            aggregated_batch_size
                        )  # [N, D, D]
                    # Save GGN samples on disk, if needed aggregated batch size reached
                    if aggregated_batch_size in ggn_batch_sizes:
                        save_results(
                            GGN_samples, n_steps, results_path, batch_size=aggregated_batch_size
                        )

                    # Compute total GGN as running average to save memory
                    GGN_counter += GGN.shape[0]
                    if GGN_total is None:
                        GGN_total = jnp.mean(GGN, axis=0)  # [D, D]
                    else:
                        GGN_total = (
                            GGN_total + jnp.sum(GGN - GGN_total[None, :, :], axis=0) / GGN_counter
                        )  # [D, D]

                # Save total GGN on disk
                save_results(GGN_total, n_steps, results_path)
                GGN_iter_counter += 1

            # Perform training step
            state, loss, n_correct = train_step(state, batch)
            # Update running statistics
            loss_epoch.append(loss)
            n_correct_epoch += n_correct
            N = batch[0].shape[0]
            n_steps += 1
            # Update progress bar
            pbar.update()
            pbar_stats["loss"] = round(float(jnp.mean(loss)), 6)
            pbar_stats["acc"] = round(n_correct / N, 4)
            pbar.set_postfix(pbar_stats)

    # Compute final epoch statistics: Epoch loss, epoch accuracy
    loss = jnp.mean(jnp.concatenate(loss_epoch)).item()  # [1]
    accuracy = float(n_correct_epoch / len(train_dataloader.dataset))  # type: ignore

    return state, loss, accuracy, n_steps, n_ggn_iterations - GGN_iter_counter


def test_epoch(
    state: TrainState,
    test_dataloader: DataLoader,
    no_progress_bar: bool,
) -> Tuple[float, float]:
    """
    Performs a single training epoch.

    Args:
        state (TrainState): Current training state.
        test_dataloader (DataLoader): Data loader for model training.
        no_progress_bar (bool): Disables progress bar.

    Returns:
        Tuple[float, float]:
            Epoch loss,
            epoch accuracy.
    """

    # Running statistics
    loss_epoch = []  # Per-item losses per test steps
    n_correct_epoch = 0  # Number of correct predictions across the epoch

    # Start epoch
    pbar_stats = {"loss": 0.0, "acc": 0.0}
    with tqdm(
        total=len(test_dataloader), desc="Test-step", disable=no_progress_bar, postfix=pbar_stats
    ) as pbar:
        # Iterate over dataset for testing
        for batch in test_dataloader:
            # Perform test step
            loss, n_correct = test_step(state, batch)
            # Update running statistics
            loss_epoch.append(loss)
            n_correct_epoch += n_correct
            N = batch[0].shape[0]
            # Update progress bar
            pbar.update()
            pbar_stats["loss"] = round(float(jnp.mean(loss)), 6)
            pbar_stats["acc"] = round(n_correct / N, 4)
            pbar.set_postfix(pbar_stats)

    # Compute final epoch statistics: Epoch loss, epoch accuracy
    loss = jnp.mean(jnp.concatenate(loss_epoch)).item()  # [1]
    accuracy = float(n_correct_epoch / len(test_dataloader.dataset))  # type: ignore

    return loss, accuracy
