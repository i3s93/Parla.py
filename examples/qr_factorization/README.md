# Blocked QR Factorization Example
**Various implementations of a blocked QR factorization to test the performance of various features.**

### Blocked QR Factorization
Given a matrix A , a [QR factorization](https://en.wikipedia.org/wiki/QR_decomposition) is the decomposition
of the matrix A into the matrix product A = QR where Q
is comprised entirely of orthogonal unit vectors and R is an
upper triangular matrix. It's a candidate for testing nested parallelism because blocks can be processed in parallel, and elements within blocks can be processed in parallel. For more information see "[Direct QR factorizations for tall-and-skinny matrices in
MapReduce architectures](https://arxiv.org/abs/1301.1071)."

### Files
- `README.md`
	- This
- `parla_source.sh`
	- Run `source parla_source.sh` before trying to use `parla.sh`.
- `parla.sh`
	- Used to run Parla-dependent programs in place of calling `python`. Required for files denoted with *.
- `qr-numpy.py`
	- Tests NumPy's basic factorization algorithm, `numpy.linalg.qr()`, which has a single level of parallelism and which all the other versions depend on.
- `qr-dask.py`
	- Tests Dask's blocked version, `dask.linalg.qr()`, highly optimized and used as a comparison for our implementations.
- `qr-simple-blocked.py`
	- Proof of concept for blocked implementation. Doesn't actually parallelize over blocks.
- `qr-multithread.py`
	- Attempt to achieve nested parallelism by spawning multiple threads. Doesn't actually work, as all NumPy calls are multiplexed onto a single group of threads (obviating the need for VECs to manage different contexts if parallelism is to be achieved in Python with threads.)
- `qr-multiprocess.py`
	- Achieves nested parallelism by spawning multiple processes, requiring data to be copied.
- `qr-multiprocess-smem-attempt.py`
	- Attempt to modify the above version to use shared memory rather than copying; incomplete.
- `qr-vec.py` *
	- Uses VECs (Virtual Execution Contexts) in order to achieve nested parallelism using multiple threads in a single virtual address space.