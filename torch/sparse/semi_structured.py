import warnings
from collections import namedtuple
from typing import Any, Optional, Tuple, List

import torch
from torch.sparse._semi_structured_conversions import (sparse_semi_structured_from_dense_cutlass, sparse_semi_structured_to_dense_cutlass)
from functools import partial

from torch.sparse._semi_structured_ops import *
import contextlib

__all__ = [
    "SparseSemiStructuredTensor",
    "SparseSemiStructuredTensorCUTLASS",
    "SparseSemiStructuredTensorCUSPARSELT",
    "to_sparse_semi_structured",
]

_SEMI_STRUCTURED_SPARSE_CONFIG = namedtuple(
    "_SEMI_STRUCTURED_SPARSE_CONFIG", "sparse_min_rows sparse_min_cols dense_min_rows dense_min_cols"
)
_AUTOPAD_DENSE = False

class SparseSemiStructuredTensor(torch.Tensor):
    _FORCE_CUTLASS = True
    _PROTOTYPE_WARNING_SHOWN = False

    # needed because of imports
    @classmethod
    def _load_dispatch_table(cls, custom_dispatch_table=None):
        if getattr(cls, 'SPARSE24_DISPATCH', None) is None:
            cls.SPARSE24_DISPATCH = {
                torch.ops.aten.values: sparse_24_values,
                torch.ops.aten.indices: sparse_24_indices,
                torch.ops.aten.is_same_size: fallback_dispatcher,
                torch.ops.aten.detach_: fallback_dispatcher,
                torch.ops.aten.detach: sparse24_detach,
                torch.ops.aten.t: sparse24_t,
                torch.ops.aten.view: sparse24_view,
                torch.ops.aten.mm: sparse24_mm,
                torch.ops.aten.matmul: sparse24_mm,
                torch.ops.aten.addmm: sparse24_addmm,
                torch.ops.aten.linear: sparse24_linear,
            }

            if custom_dispatch_table:
                for op in custom_dispatch_table:
                    cls.SPARSE24_DISPATCH[op] = custom_dispatch_table[op]

    @staticmethod
    def __new__(
        cls,
        shape,
        packed: Optional[torch.Tensor],
        meta: Optional[torch.Tensor],
        packed_t: Optional[torch.Tensor],
        meta_t: Optional[torch.Tensor],
        threads_masks: Optional[torch.Tensor],
        transposed: bool = False,
        fuse_transpose_cusparselt: bool = False,
        alg_id_cusparselt: int = 0,
        requires_grad: bool = False,
    ):
        if not cls._PROTOTYPE_WARNING_SHOWN:
            warnings.warn(
                (
                    "The PyTorch API of SparseSemiStructuredTensor is in prototype stage "
                    "and will change in the near future. Please open a Github issue "
                    "for features requests and see our documentation on the torch.sparse "
                    "module for further information about the project."
                ),
                UserWarning,
            )
            cls._PROTOTYPE_WARNING_SHOWN = True

        previous_tensor = packed if packed is not None else packed_t

        kwargs = {}
        kwargs["device"] = previous_tensor.device  # type: ignore[assignment]
        kwargs["dtype"] = previous_tensor.dtype  # type: ignore[assignment]
        kwargs["requires_grad"] = requires_grad

        tensor = torch.Tensor._make_wrapper_subclass(cls, shape, **kwargs)  # type: ignore[attr-defined]

        tensor.packed = packed
        tensor.meta = meta
        tensor.packed_t = packed_t
        tensor.meta_t = meta_t
        tensor.threads_masks = threads_masks

        tensor.transposed = transposed
        tensor.fuse_transpose_cusparselt = fuse_transpose_cusparselt
        tensor.alg_id_cusparselt = alg_id_cusparselt

        cls._load_dispatch_table()

        return tensor

    @classmethod
    def _validate_device_dim_dtype_shape(cls, original_tensor) -> None:
        # check device
        if not original_tensor.is_cuda:
            raise RuntimeError(
                f"Error original_tensor.device= {original_tensor.device} is not supported! "
                "Only CUDA tensors are currently supported."
            )

        # check dim
        if original_tensor.dim() != 2:
            raise RuntimeError(
                f"Error original_tensor.dim = {original_tensor.dim()} is not supported! "
                "Only 2d tensors are currently supported."
            )

        # check contiguous
        if not original_tensor.is_contiguous():
            raise RuntimeError(
                "Error original_tensor is not contiguous!"
                "Only contiguous tensors are currently supported."
            )

        # check dtype
        if original_tensor.dtype not in cls._DTYPE_SHAPE_CONSTRAINTS:
            raise RuntimeError(
                f"Error original_tensor.dtype {original_tensor.dtype} is not a supported dtype! "
                "dtype must be one of: {cls._DTYPE_SHAPE_CONSTRAINTS}"
            )

        # check shape
        m, n = original_tensor.shape
        min_rows = cls._DTYPE_SHAPE_CONSTRAINTS[original_tensor.dtype].sparse_min_rows
        min_cols = cls._DTYPE_SHAPE_CONSTRAINTS[original_tensor.dtype].sparse_min_cols
        if m < min_rows or m % min_rows or n < min_cols or n % min_cols:
            # TODO in the future we can add in padding to support sparse dimensions that aren't perfect multiples
            raise RuntimeError(
                f"Error original_tensor.shape {original_tensor.shape} is not supported! "
                f"Both dimensions must be larger or equal than and a multiple of ({min_rows}, {min_cols})"
            )

    @classmethod
    def _pad_dense_input(cls, dense_input: torch.Tensor) -> torch.Tensor:
        """
        Calculates padding for dense tensor and pads tensor if necessary.
        If padding is not required, this function returns the original tensor.
        """
        # only 2d matmul
        assert dense_input.dim() == 2

        # check shape
        m, n = dense_input.shape
        min_rows = cls._DTYPE_SHAPE_CONSTRAINTS[dense_input.dtype].dense_min_rows
        min_cols = cls._DTYPE_SHAPE_CONSTRAINTS[dense_input.dtype].dense_min_cols

        # calculate padding
        to_pad_m = -m % min_rows if m < min_rows or m % min_rows else 0
        to_pad_n = -n % min_cols if n < min_cols or n % min_rows else 0
        if to_pad_m or to_pad_n:
            return torch.nn.functional.pad(dense_input, (0, to_pad_n, 0, to_pad_m))
        else:
            return dense_input

    def __repr__(self) -> str:  # type: ignore[override]
        assert hasattr(self, "shape")
        assert hasattr(self, "transposed")
        return (
            f"{self.__class__.__name__}(shape={self.shape}, "
            f"transposed={self.transposed})"
        )
    def __tensor_flatten__(self) -> Tuple[List[str], Tuple[torch.Size, bool]]:

        strings = []

        for s in ["packed", "meta", "packed_t", "meta_t", "threads_masks"]:
            if getattr(self, s) is not None:
                strings.append(s)
        return strings, (
            self.shape,
            self.transposed,
            self.fuse_transpose_cusparselt,
            self.alg_id_cusparselt,
            self.requires_grad,
        )

    @classmethod
    def __tensor_unflatten__(
        cls,
        inner_tensors,
        tensor_meta,
        outer_size,
        outer_stride
    ):
        packed       = inner_tensors.get("packed", None)
        meta         = inner_tensors.get("meta", None)
        packed_t     = inner_tensors.get("packed_t", None)
        meta_t       = inner_tensors.get("meta_t", None)
        threads_masks= inner_tensors.get("threads_masks", None)

        shape, transposed, fuse_transpose_cusparselt, alg_id_cusparselt, requires_grad = tensor_meta

        return cls(
            shape=shape,
            transposed=transposed,
            packed=packed,
            meta=meta,
            packed_t=packed_t,
            meta_t=meta_t,
            threads_masks=threads_masks,
            fuse_transpose_cusparselt=fuse_transpose_cusparselt,
            alg_id_cusparselt=alg_id_cusparselt,
            requires_grad=requires_grad
        )


    def _sp24_to_dense(self) -> torch.Tensor:
        # Multiply by identity
        # WARN: This is not efficient at all
        e = torch.eye(
            self.shape[1], self.shape[1], device=self.device, dtype=self.dtype
        )
        return self @ e

    __torch_function__ = torch._C._disabled_torch_function_impl

    @classmethod
    def __torch_dispatch__(cls, func, types, args, kwargs) -> Any:
        if func._overloadpacket not in cls.SPARSE24_DISPATCH:
            raise NotImplementedError(
                f"{cls.__name__} only supports a specific set of operations, "
                f"can't perform requested op ({func.__name__})"
            )
        return cls.SPARSE24_DISPATCH[func._overloadpacket](
            func, types, args, kwargs
        )

class SparseSemiStructuredTensorCUTLASS(SparseSemiStructuredTensor):

    _DTYPE_SHAPE_CONSTRAINTS = {
        torch.int8: _SEMI_STRUCTURED_SPARSE_CONFIG(16, 128, 16, 16),
        torch.float16: _SEMI_STRUCTURED_SPARSE_CONFIG(32, 64, 8, 8),
        torch.bfloat16: _SEMI_STRUCTURED_SPARSE_CONFIG(32, 64, 8, 8),
        torch.float32: _SEMI_STRUCTURED_SPARSE_CONFIG(32, 32, 4, 4),
    }

    @classmethod
    def from_dense(cls, original_tensor):
        cls._validate_device_dim_dtype_shape(original_tensor)
        sparse_tensor_cutlass, meta_tensor_cutlass = sparse_semi_structured_from_dense_cutlass(original_tensor)
        return cls(original_tensor.shape,
                   packed=sparse_tensor_cutlass,
                   meta=meta_tensor_cutlass,
                   packed_t=None,
                   meta_t=None,
                   threads_masks=None,
                   requires_grad=original_tensor.requires_grad)

    def to_dense(self):
        return sparse_semi_structured_to_dense_cutlass(
            self.packed,
            self.meta,
        )

    def _mm(
        self,
        B: torch.Tensor,
        *,
        bias: Optional[torch.Tensor] = None,
        prefer_col_major_output: bool = False,
    ) -> torch.Tensor:
        if isinstance(B, SparseSemiStructuredTensor):
            raise ValueError(
                "`Sparse24Tensor @ Sparse24Tensor` is not supported by the hardware"
            )
        if self.ndim != 2 or B.ndim != 2:
            raise NotImplementedError(
                f"`{self.__class__.__name__}` matmul: Broadcasting is not implemented"
            )
        assert self.packed is not None, "FLAG"

        res = torch._sparse_semi_structured_linear(
            B.t(),
            self.packed,
            self.meta,
            bias=bias).t()
        return res[: self.shape[0]]


class SparseSemiStructuredTensorCUSPARSELT(SparseSemiStructuredTensor):

    _DTYPE_SHAPE_CONSTRAINTS = {
        torch.int8: _SEMI_STRUCTURED_SPARSE_CONFIG(32, 32, 16, 16),
        torch.float16: _SEMI_STRUCTURED_SPARSE_CONFIG(16, 16, 8, 8),
        torch.bfloat16: _SEMI_STRUCTURED_SPARSE_CONFIG(16, 16, 8, 8),
        torch.float32: _SEMI_STRUCTURED_SPARSE_CONFIG(8, 8, 4, 4),
    }

    _FUSE_TRANSPOSE = False
    _DEFAULT_ALG_ID = 0


    @classmethod
    def from_dense(cls, original_tensor):
        cls._validate_device_dim_dtype_shape(original_tensor)
        compressed_tensor_cusparselt = torch._cslt_compress(original_tensor)
        return cls(original_tensor.shape,
                   packed=compressed_tensor_cusparselt,
                   meta=None,
                   packed_t=None,
                   meta_t=None,
                   threads_masks=None,
                   requires_grad=original_tensor.requires_grad)

    def to_dense(self):
        col = self.shape[-1]
        return torch.mm(self, torch.eye(col, dtype=self.dtype, device=self.device))

    def _mm(
        self,
        B: torch.Tensor,
        *,
        bias: Optional[torch.Tensor] = None,
        prefer_col_major_output: bool = False,
    ) -> torch.Tensor:
        if isinstance(B, SparseSemiStructuredTensor):
            raise ValueError(
                "`Sparse24Tensor @ Sparse24Tensor` is not supported by the hardware"
            )
        if self.ndim != 2 or B.ndim != 2:
            raise NotImplementedError(
                f"`{self.__class__.__name__}` matmul: Broadcasting is not implemented"
            )
        if B.dtype != self.dtype:
            raise NotImplementedError(
                f"`{self.__class__.__name__}` matmul: trying to do `A={tuple(self.shape)} @ B={tuple(B.shape)}`, "
                f"with A.dtype={self.dtype} and B.dtype={B.dtype}. "
                "This operation is only supported when A and B have the same data type."
            )
        if bias is not None and bias.dtype != self.dtype:
            raise NotImplementedError(
                f"`{self.__class__.__name__}` matmul: trying to do `A={tuple(self.shape)} @ B={tuple(B.shape)} + C`, "
                "with A.dtype=B.dtype={self.dtype} and C.dtype={B.dtype}. "
                "This operation is only supported when A, B and C have the same data type."
            )

        assert self.packed is not None, "FLAG"
        temp = torch._cslt_sparse_mm(
            self.packed,
            B,
            bias=bias,
            transpose_result=prefer_col_major_output)
        if prefer_col_major_output:
            temp = temp.t()
        return temp


def to_sparse_semi_structured(
    original_tensor: torch.Tensor,
) -> Any:
    """
    This function converts a dense tensor into a sparse semi-structured tensor.
    It will return either
        1. a SparseSemiStructuredTensor if the input tensor is already in the correct format
        2. a regular SparseTensor if the input tensor was not in the correct format

    This function will check to ensure the dense tensor has the right dtype, size, dims, and device.
    We currently only support semi-structured sparse tensors for 2d CUDA tensors.
    Additionally, your tensor must be a positive multiple of a block size given the dtype

    - torch.float16  (r, c) must be >= and a multiple of 64
    - torch.int8     (r, c) must be >= and a multiple of 128

    Args:
        original_tensor (Tensor): the dense tensor to convert

    Returns:
        SparseSemiStructuredTensor: A sparse semi-structured tensor created from the given original_tensor

    Raises:
        None

    Example:
        >>> # xdoctest: +REQUIRES(env:TORCH_DOCTEST_CUDA)
        >>> A = torch.Tensor([0, 0, 1, 1]).tile((128, 32)).half().cuda()
        tensor([[0., 0., 1.,  ..., 0., 1., 1.],
                [0., 0., 1.,  ..., 0., 1., 1.],
                [0., 0., 1.,  ..., 0., 1., 1.],
                ...,
                [0., 0., 1.,  ..., 0., 1., 1.],
                [0., 0., 1.,  ..., 0., 1., 1.],
                [0., 0., 1.,  ..., 0., 1., 1.]], device='cuda:0', dtype=torch.float16)
        >>> A_sparse = to_sparse_semi_structured(A)
        SparseSemiStructuredTensor(shape=torch.Size([128, 128]), transposed=False, values=tensor([[1., 1., 1.,  ..., 1., 1., 1.],
                [1., 1., 1.,  ..., 1., 1., 1.],
                [1., 1., 1.,  ..., 1., 1., 1.],
                ...,
                [1., 1., 1.,  ..., 1., 1., 1.],
                [1., 1., 1.,  ..., 1., 1., 1.],
                [1., 1., 1.,  ..., 1., 1., 1.]], device='cuda:0', dtype=torch.float16),
            metadata=tensor([[-4370, -4370, -4370,  ..., -4370, -4370, -4370],
                [-4370, -4370, -4370,  ..., -4370, -4370, -4370],
                [-4370, -4370, -4370,  ..., -4370, -4370, -4370],
                ...,
                [-4370, -4370, -4370,  ..., -4370, -4370, -4370],
                [-4370, -4370, -4370,  ..., -4370, -4370, -4370],
                [-4370, -4370, -4370,  ..., -4370, -4370, -4370]], device='cuda:0',
       dtype=torch.int16))
    """
    sparse_subclass = SparseSemiStructuredTensorCUTLASS if SparseSemiStructuredTensor._FORCE_CUTLASS else SparseSemiStructuredTensorCUSPARSELT
    return sparse_subclass.from_dense(original_tensor)


# OPS


@contextlib.contextmanager
def no_dispatch():
    guard = torch._C._DisableTorchDispatch()
    try:
        yield
    finally:
        del guard

def fallback_dispatcher(func, types, args, kwargs):
    with no_dispatch():
        return func(*args)

def sparse_24_values(func, types, args=(), kwargs=None) -> torch.Tensor:
    assert len(args) == 1
    A = args[0]
    assert isinstance(A, SparseSemiStructuredTensor)
    if A.meta is None:
        m, k = A.shape
        num_kept_elements = m * k // 2
        return A.packed[:num_kept_elements:].view(m, -1)
    else:
        return A.packed.detach()

def sparse_24_indices(func, types, args=(), kwargs=None) -> torch.Tensor:
    assert len(args) == 1
    A = args[0]
    assert isinstance(A, SparseSemiStructuredTensor)
    if A.meta is None:
        m, k = A.shape
        num_kept_elements = m * k // 2
        metadata = A.packed[num_kept_elements:].view(m, -1)
        return metadata.view(torch.int32 if A.dtype == torch.int32 else torch.int16)
    else:
        return A.meta


def sparse24_t(func, types, args=(), kwargs=None) -> torch.Tensor:
    assert len(args) == 1
    self = args[0]
    assert isinstance(self, SparseSemiStructuredTensor)
    assert len(self.shape) == 2
    # Because we cannot go from the compressed representation back to the dense representation currently,
    # we just keep track of how many times we have been transposed. Depending on whether the sparse matrix
    # is the first or second argument, we expect an even / odd number of calls to transpose respectively.
    return self.__class__(
        (self.shape[-1], self.shape[0]),
        packed=self.packed_t,
        meta=self.meta_t,
        packed_t=self.packed,
        meta_t=self.meta,
        threads_masks=self.threads_masks.transpose(0, 1) if self.threads_masks is not None else None,
        fuse_transpose_cusparselt=args[0].fuse_transpose_cusparselt,
        alg_id_cusparselt=args[0].alg_id_cusparselt,
    )

def sparse24_view(func, types, args=(), kwargs=None) -> torch.Tensor:
    assert len(args) == 2
    self, shape = args
    if tuple(shape) != self.shape:
        raise NotImplementedError(
            f"`view` is not implemented for SparseSemiStructuredTensor, except for the dummy case (shape={shape})"
        )
    return self


def sparse24_detach(func, types, args, kwargs) -> torch.Tensor:
    assert len(args) == 1
    self = args[0]
    return self.__class__(
        shape=self.shape,
        packed=self.packed,
        meta=self.meta,
        packed_t=self.packed_t,
        meta_t=self.meta_t,
        threads_masks=self.threads_masks,
        requires_grad=False,
    )

def sparse24_mm(func, types, args=(), kwargs=None) -> torch.Tensor:
    assert len(args) == 2
    A, B = args
    if A.ndim != 2 or B.ndim != 2:
        raise NotImplementedError(
            "`SparseSemiStructuredTensor` matmul: Broadcasting is not implemented"
        )
    if isinstance(A, SparseSemiStructuredTensor):
        row, col = B.shape
        B_padded = A._pad_dense_input(B)
        res = A._mm(B_padded)
        return res[:, :col]
    else:
        B_t = B.t()
        assert isinstance(B_t, SparseSemiStructuredTensor)
        row, col = A.shape
        A_padded = B._pad_dense_input(A)
        res = B_t._mm(A_padded.t()).t()
        return res[:row, :]



def sparse24_addmm(func, types, args=(), kwargs=None) -> torch.Tensor:
    assert len(args) == 3
    bias, A, B = args
    if A.ndim != 2 or B.ndim != 2:
        raise NotImplementedError(
            "`SparseSemiStructuredTensor` matmul: Broadcasting is not implemented"
        )
    if bias.ndim != 1:
        raise NotImplementedError(
            f"`SparseSemiStructuredTensor` matmul: only bias dim=1 supported. Shape={bias.shape}"
        )
    if isinstance(A, SparseSemiStructuredTensor):
        raise NotImplementedError(
            "`SparseSemiStructuredTensor` matmul: only operand B of `addmm` can be sparse"
        )
    B_t = B.t()
    assert isinstance(B_t, SparseSemiStructuredTensor)
    row, col = A.shape
    A_padded = B_t._pad_dense_input(A)
    result = B_t._mm(A_padded.t(), bias=bias).t()
    return result[:row, :]


def sparse24_linear(func, types, args=(), kwargs=None) -> torch.Tensor:
    assert len(args) in [2, 3]
    A, B = args[:2]
    bias = args[2] if len(args) == 3 else None

    shape = A.shape
    A_2d = A.view(-1, shape[-1])

    if bias is None:
        res = A_2d @ B.t()
    else:
        res = sparse24_addmm(
            func=None,
            types=None,
            args=[bias, A_2d, B.t()],
        )

    return res.view(*shape[:-1], -1)
