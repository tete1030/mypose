#include <ATen/ATen.h>

#include <cuda.h>
#include <cuda_runtime.h>
#include <iostream>

#include "lacorr2d_kernel.cuh"

using std::cout;

// CC61
#define NUM_BANK 32
#define HALF_WARP 16
#define MAX_NUM_THREADS_PER_BLOCK 1024
#define MAX_SIZE_SMEM_PER_BLOCK 49152
// we use half to maximize occupancy
#define MAX_NUM_REGISTERS_PER_BLOCK 32768 //65536

// -ptxas-options=-v
#define NUM_REGISTER_FORWARD_FLOAT 28
#define NUM_REGISTER_BACKWARD_FLOAT 32
#define NUM_REGISTER_FORWARD_DOUBLE 30
#define NUM_REGISTER_BACKWARD_DOUBLE 32

#define NUM_REGISTER(NAME, DIR, TYPE)                                           \
  [&] {                                                                         \
    const at::Type& the_type = TYPE;                                            \
    switch (the_type.scalarType()) {                                            \
        case at::ScalarType::Float: return NUM_REGISTER_##DIR##_FLOAT;          \
        case at::ScalarType::Double: return NUM_REGISTER_##DIR##_DOUBLE;        \
        default:                                                                \
          AT_ERROR(#NAME, " not implemented for '", the_type.toString(), "'");  \
    }                                                                           \
  }()

#define CONDITION_INSIDE(X, Y, W, H) ((Y) >= 0 && (Y) < (H) && (X) >= 0 && (X) < (W))
#define CONDITION_UP_true(OFFSET, UBOUND) ((OFFSET) < (UBOUND)) &&
#define CONDITION_UP_false(OFFSET, UBOUND)
#define STRINGIFY(X) #X

#define AT_SIZEOF_FLOATING_TYPES(TYPE, NAME)                                  \
  [&] {                                                                       \
    const at::Type& the_type = TYPE;                                          \
    switch (the_type.scalarType()) {                                          \
      case at::ScalarType::Double: return sizeof(double);                     \
      case at::ScalarType::Float: return sizeof(float);                       \
      default:                                                                \
        AT_ERROR(#NAME, " not implemented for '", the_type.toString(), "'");  \
    }                                                                         \
  }()

#define gpuErrchk(ans) { gpuAssert((ans), __FILE__, __LINE__); }
inline void gpuAssert(cudaError_t code, const char *file, int line, bool abort=true)
{
    if (code != cudaSuccess)
    {
        fprintf(stderr,"GPUassert: %s %s %d\n", cudaGetErrorString(code), file, line);
        if (abort) exit(code);
    }
}

template <typename scalar_t>
__global__ void lacorr2d_forward_cuda_kernel(
    const scalar_t* __restrict__ input,
    scalar_t* __restrict__ output,
    const int kernel_height,
    const int kernel_width,
    const int stride_height,
    const int stride_width,
    const int corr_off_y,
    const int corr_off_x,
    const int n_corr_h,
    const int n_corr_w,
    const int total_channel,
    const int channel_size,
    const int height,
    const int width) {
        extern __shared__ unsigned char s[];

        int ichan = blockIdx.x * blockDim.y + threadIdx.y;
        
        int ithread = threadIdx.x;
        int icorr = blockIdx.y;

        int y_k = ithread / kernel_width;
        int x_k = ithread % kernel_width;

        int y_corr = icorr / n_corr_w;
        int x_corr = icorr % n_corr_w;

        // left and top conner of current corr in input image
        int left_k = corr_off_x + stride_width * x_corr;
        int top_k = corr_off_y + stride_height * y_corr;

        int half_kh = kernel_height / 2;
        int half_kw = kernel_width / 2;

        int bg_width = kernel_width * 2 - 1;
        int bg_height = kernel_height * 2 - 1;

        input += ichan * height * width;
        output += (((((ichan / channel_size) * n_corr_h + y_corr) * n_corr_w + x_corr) * channel_size + ichan % channel_size) * kernel_height + y_k) * kernel_width + x_k;

        scalar_t *inp_smem = reinterpret_cast<scalar_t*>(s) + threadIdx.y * bg_width * bg_height;

        if (ichan < total_channel) {
            int i_inp_smem = INDEX2D(2*x_k, 2*y_k, bg_width);
            int y_inp = top_k - half_kh + 2*y_k;
            int x_inp = left_k - half_kw + 2*x_k;
            int i_inp = INDEX2D(x_inp, y_inp, width);

            #define INIT_INP(X_INP, Y_INP, COND_X, COND_Y) \
                if(CONDITION_UP_##COND_X(X_INP, left_k-half_kw+bg_width) CONDITION_UP_##COND_Y(Y_INP, top_k-half_kh+bg_height) true) { \
                    if (CONDITION_INSIDE((X_INP), (Y_INP), width, height)) { \
                        inp_smem[i_inp_smem] = input[i_inp]; \
                    } else { \
                        inp_smem[i_inp_smem] = 0; \
                    } \
                }

            INIT_INP(x_inp, y_inp, false, false)
            i_inp += 1;
            i_inp_smem += 1;
            INIT_INP(x_inp+1, y_inp, true, false)
            i_inp += width - 1;
            i_inp_smem += bg_width - 1;
            INIT_INP(x_inp, y_inp+1, false, true)
            i_inp += 1;
            i_inp_smem += 1;
            INIT_INP(x_inp+1, y_inp+1, true, true)
            i_inp -= width + 1;
            i_inp_smem -= bg_width + 1;

            #undef INIT_INP

        }

        __syncthreads();

        // This loop respect to
        // - [offset inside a kernel],
        // - [offset in a kernel projection on background].
        //
        // - [offset in a output],
        // - [location of whole kernel projection on background]

        if (ichan < total_channel) {
            scalar_t out_reg = 0.;

            int i_bg = INDEX2D(x_k, y_k, bg_width);
            int i_k = INDEX2D(half_kw, half_kh, bg_width);
            for (int y_off=0; y_off < kernel_height; y_off++) {
                for (int x_off=0; x_off < kernel_width; x_off++) {
                    out_reg += inp_smem[i_bg] * inp_smem[i_k];
                    i_bg += 1;
                    i_k += 1;
                }
                i_bg += bg_width - kernel_width;
                i_k += bg_width - kernel_width;
            }

            *output = out_reg;
        }
}

// cc61:
// - maximum threads 1024 per block
// - maximum resident threads 2048 per SM
// - maximum resident blocks 32 per SM
// - maximum resident warps 64 per SM
// 
// gtx1080: 20 SM

std::vector<at::Tensor> lacorr2d_forward_cuda(
    at::Tensor input,
    int kernel_height,
    int kernel_width,
    int stride_height,
    int stride_width,
    int padding_top,
    int padding_bottom,
    int padding_left,
    int padding_right) {
    const int batch_size = input.size(0);
    const int channel_size = input.size(1);
    const int total_channel = batch_size * channel_size;
    const int height = input.size(2);
    const int width = input.size(3);

    AT_ASSERT(kernel_width <= width, "kernel_width must be lesser than or equal to width")
    AT_ASSERT(kernel_height <= height, "kernel_height must be lesser than or equal to height")
    AT_ASSERT(stride_width <= width, "stride_width must be lesser than or equal to width")
    AT_ASSERT(stride_height <= height, "stride_height must be lesser than or equal to height")
#if FLOAT_ONLY
    AT_ASSERT(input.type().scalarType() == at::ScalarType::Float, "input.scalarType must be float")
#endif

    const int n_corr_w = (width + padding_left + padding_right - kernel_width) / stride_width + 1;
    const int n_corr_h = (height + padding_top + padding_bottom - kernel_height) / stride_height + 1;
    const int n_corr = n_corr_w * n_corr_h;
    const int kernel_size = kernel_height * kernel_width;
    const int bg_width = kernel_width * 2 - 1;
    const int bg_height = kernel_height * 2 - 1;

    int shared_memory_size = bg_width * bg_height * AT_SIZEOF_FLOATING_TYPES(input.type(), "lacorr2d_forward_cuda");
    int num_register = NUM_REGISTER("lacorr2d_forward_cuda", FORWARD, input.type()) * kernel_size;
    const int n_channel_per_block = min(min(MAX_SIZE_SMEM_PER_BLOCK / shared_memory_size, MAX_NUM_THREADS_PER_BLOCK / kernel_size), MAX_NUM_REGISTERS_PER_BLOCK / num_register);
    D(
        cout << "smem: " << shared_memory_size << '/' << MAX_SIZE_SMEM_PER_BLOCK << " | thread: " << kernel_size << '/' << MAX_NUM_THREADS_PER_BLOCK << " | register: " << num_register << '/' << MAX_NUM_REGISTERS_PER_BLOCK << std::endl;
        cout << "kernel_size: " << kernel_size << std::endl;
        cout << "n_channel_per_block: " << n_channel_per_block << std::endl;
        cout << "threads_per_block: " << kernel_size * n_channel_per_block << std::endl;
        cout << "blocks.x: " << (n_channel_per_block > 0? ((total_channel + n_channel_per_block - 1) / n_channel_per_block) : -1) << std::endl;
        cout << "blocks.y: " << n_corr << std::endl;
        cout << "shared_memory_size: " << shared_memory_size << std::endl;
        cout << "register_per_block: " << num_register * n_channel_per_block << std::endl;
    )
    shared_memory_size *= n_channel_per_block;
    AT_ASSERT(n_channel_per_block > 0, "shared_memory_size or kernel_size or num_reg exceeds limitation");

    // work on pytorch 0.4.0 , have been changed in master 07/10/2018
    auto output = at::zeros(input.type(), std::vector<int64_t>{batch_size, n_corr_h, n_corr_w, channel_size, kernel_height, kernel_width});

    // n_channel_per_block*bg_width*bg_height*sizeof(scalar_t)
    // should be less than or equal to 32768
    // when block_per_SM == 2 to maxmize occupancy

    const dim3 threads_per_block(kernel_size, n_channel_per_block);
    const dim3 blocks((total_channel + n_channel_per_block - 1) / n_channel_per_block, n_corr);

#define CALL_FORWARD() \
    lacorr2d_forward_cuda_kernel<scalar_t><<<blocks, threads_per_block, shared_memory_size>>>( \
        input.data<scalar_t>(), \
        output.data<scalar_t>(), \
        kernel_height, \
        kernel_width, \
        stride_height, \
        stride_width, \
        -padding_top, \
        -padding_left, \
        n_corr_h, \
        n_corr_w, \
        total_channel, \
        channel_size, \
        height, \
        width);

#if FLOAT_ONLY
    using scalar_t = float;
    CALL_FORWARD()
#else
    AT_DISPATCH_FLOATING_TYPES(input.type(), "lacorr2d_forward_cuda", ([&] {
        CALL_FORWARD()
    }));
#endif
    gpuErrchk(cudaPeekAtLastError());

    return {output};
}

template <typename scalar_t>
__global__ void lacorr2d_backward_cuda_kernel(
    const scalar_t* __restrict__ input,
    const scalar_t* __restrict__ grad_output,
    scalar_t* __restrict__ grad_input,
    const int kernel_height,
    const int kernel_width,
    const int stride_height,
    const int stride_width,
    const int corr_off_y,
    const int corr_off_x,
    const int n_corr_h,
    const int n_corr_w,
    const int total_channel,
    const int channel_size,
    const int height,
    const int width) {
        extern __shared__ unsigned char s[];

        int ichan = blockIdx.x * blockDim.y + threadIdx.y;
        
        int ithread = threadIdx.x;
        int icorr = blockIdx.y;

        int y_k = ithread / kernel_width;
        int x_k = ithread % kernel_width;

        int y_corr = icorr / n_corr_w;
        int x_corr = icorr % n_corr_w;

        // left and top conner of current corr in input image
        int left_k = corr_off_x + stride_width * x_corr;
        int top_k = corr_off_y + stride_height * y_corr;

        int half_kh = kernel_height / 2;
        int half_kw = kernel_width / 2;

        int bg_width = kernel_width * 2 - 1;
        int bg_height = kernel_height * 2 - 1;

        int i_inp_smem = INDEX2D(2*x_k, 2*y_k, bg_width);
        int y_inp = top_k - half_kh + 2*y_k;
        int x_inp = left_k - half_kw + 2*x_k;
        int i_inp = INDEX2D(x_inp, y_inp, width);

        input += ichan * height * width;
        grad_input += ichan * height * width;
        grad_output += (((((ichan / channel_size) * n_corr_h + y_corr) * n_corr_w + x_corr) * channel_size + ichan % channel_size) * kernel_height + y_k) * kernel_width + x_k;

        scalar_t *inp_smem = reinterpret_cast<scalar_t*>(s) + threadIdx.y * bg_width * bg_height;
        scalar_t *grad_inp_smem = inp_smem + blockDim.y * bg_width * bg_height;

        if (ichan < total_channel) {

            #define INIT_INP_GINP(X_INP, Y_INP, COND_X, COND_Y) \
                if(CONDITION_UP_##COND_X(X_INP, left_k-half_kw+bg_width) CONDITION_UP_##COND_Y(Y_INP, top_k-half_kh+bg_height) true) { \
                    if (CONDITION_INSIDE((X_INP), (Y_INP), width, height)) { \
                        inp_smem[i_inp_smem] = input[i_inp]; \
                    } else { \
                        inp_smem[i_inp_smem] = 0.; \
                    } \
                    grad_inp_smem[i_inp_smem] = 0.; \
                }

            INIT_INP_GINP(x_inp, y_inp, false, false)
            i_inp += 1;
            i_inp_smem += 1;
            INIT_INP_GINP(x_inp+1, y_inp, true, false)
            i_inp += width - 1;
            i_inp_smem += bg_width - 1;
            INIT_INP_GINP(x_inp, y_inp+1, false, true)
            i_inp += 1;
            i_inp_smem += 1;
            INIT_INP_GINP(x_inp+1, y_inp+1, true, true)
            i_inp -= width + 1;
            i_inp_smem -= bg_width + 1;

            #undef INIT_INP_GINP

        }

        __syncthreads();

        // Calculate background grad
        // We separate background grad and kernel grad to avoid severe Bank Conflict.
        // This loop respect to
        // - [offset inside a kernel],
        // - [offset in a kernel projection on background].
        //
        // - [offset in a output],
        // - [location of whole kernel projection on background]
        // is fixed in this thread.

        scalar_t grad_out_reg;

        if (ichan < total_channel) {
            grad_out_reg = *grad_output;

            grad_inp_smem += y_k * bg_width + x_k;
            inp_smem += half_kh * bg_width + half_kw;

            for (int y_off=0; y_off < kernel_height; y_off++) {
                for (int x_off=0; x_off < kernel_width; x_off++) {
                    // atomicAdd is in case of overlapping maps
                    atomicAdd(grad_inp_smem, grad_out_reg * (*inp_smem));
                    grad_inp_smem += 1;
                    inp_smem += 1;
                }
                grad_inp_smem += bg_width - kernel_width;
                inp_smem += bg_width - kernel_width;
            }
            grad_inp_smem -= (kernel_height + y_k) * bg_width + x_k;
            inp_smem -= (kernel_height + half_kh) * bg_width + half_kw;
        }

        __syncthreads();

        // Store background grad, reuse its shared memory for storing output_grad

        if (ichan < total_channel) {

            #define STORE_GRADINP(X_INP, Y_INP, COND_X, COND_Y) \
                if (CONDITION_UP_##COND_X(X_INP, left_k-half_kw+bg_width) CONDITION_UP_##COND_Y(Y_INP, top_k-half_kh+bg_height) CONDITION_INSIDE((X_INP), (Y_INP), width, height)) { \
                    atomicAdd(&grad_input[i_inp], grad_inp_smem[i_inp_smem]); \
                }
            
            STORE_GRADINP(x_inp, y_inp, false, false)
            i_inp += 1;
            i_inp_smem += 1;
            STORE_GRADINP(x_inp+1, y_inp, true, false)
            i_inp += width - 1;
            i_inp_smem += bg_width - 1;
            STORE_GRADINP(x_inp, y_inp+1, false, true)
            i_inp += 1;
            i_inp_smem += 1;
            STORE_GRADINP(x_inp+1, y_inp+1, true, true)
            i_inp -= width + 1;
            i_inp_smem -= bg_width + 1;

            #undef STORE_GRADINP
        }

        __syncthreads();

        // Store read grad_out into shared memory

        // Use grad_inp_smem as grad_out_smem
        scalar_t *grad_out_smem = grad_inp_smem;

        if (ichan < total_channel) {
            grad_out_smem[ithread] = grad_out_reg;
        }

        __syncthreads();

        // Calculate kernel grad and store it into global memory
        // We separate background grad and kernel grad to avoid severe Bank Conflict
        // The following loop is different from the loop above in their meaning.
        // The loop respect to
        // - [location of whole kernel projection on background],
        // - [offset in a output].
        //
        // - [offset in a kernel],
        // - [offset in a kernel projection on background]
        // is fixed in this thread.

        scalar_t grad_inp_reg = 0.;

        if (ichan < total_channel && CONDITION_INSIDE(left_k+x_k, top_k+y_k, width, height)) {
            inp_smem += y_k * bg_width + x_k;
            for (int y_off=0; y_off < kernel_height; y_off++) {
                for (int x_off=0; x_off < kernel_width; x_off++) {
                    grad_inp_reg += (*grad_out_smem) * (*inp_smem);
                    inp_smem += 1;
                    grad_out_smem += 1;
                }
                inp_smem += bg_width - kernel_width;
                // grad_out_smem += kernel_width - kernel_width;
            }
            inp_smem -= (kernel_height + y_k) * bg_width + x_k;
            grad_out_smem -= kernel_height * kernel_width;

            // atomicAdd is used because of potential overlapping maps
            atomicAdd(&grad_input[INDEX2D(left_k+x_k, top_k+y_k, width)], grad_inp_reg);
        }

}

std::vector<at::Tensor> lacorr2d_backward_cuda(
    at::Tensor input,
    at::Tensor grad_output,
    int kernel_height,
    int kernel_width,
    int stride_height,
    int stride_width,
    int padding_top,
    int padding_bottom,
    int padding_left,
    int padding_right) {
    const int batch_size = input.size(0);
    const int channel_size = input.size(1);
    const int total_channel = batch_size * channel_size;
    const int height = input.size(2);
    const int width = input.size(3);

    AT_ASSERT(kernel_width <= width, "kernel_width must be lesser than or equal to width")
    AT_ASSERT(kernel_height <= height, "kernel_height must be lesser than or equal to height")
    AT_ASSERT(stride_width <= width, "stride_width must be lesser than or equal to width")
    AT_ASSERT(stride_height <= height, "stride_height must be lesser than or equal to height")
#if FLOAT_ONLY
    AT_ASSERT(input.type().scalarType() == at::ScalarType::Float, "input.scalarType must be float")
    AT_ASSERT(grad_output.type().scalarType() == at::ScalarType::Float, "grad_output.scalarType must be float")
#endif

    const int n_corr_w = (width + padding_left + padding_right - kernel_width) / stride_width + 1;
    const int n_corr_h = (height + padding_top + padding_bottom - kernel_height) / stride_height + 1;
    const int n_corr = n_corr_w * n_corr_h;
    const int kernel_size = kernel_height * kernel_width;
    const int bg_width = kernel_width * 2 - 1;
    const int bg_height = kernel_height * 2 - 1;

    int shared_memory_size = 2 * bg_width * bg_height * AT_SIZEOF_FLOATING_TYPES(input.type(), "lacorr2d_backward_cuda");
    int num_register = NUM_REGISTER("lacorr2d_backward_cuda", BACKWARD, input.type()) * kernel_size;
    const int n_channel_per_block = min(min(MAX_SIZE_SMEM_PER_BLOCK / shared_memory_size, MAX_NUM_THREADS_PER_BLOCK / kernel_size), MAX_NUM_REGISTERS_PER_BLOCK / num_register);
    D(
        cout << "smem: " << shared_memory_size << '/' << MAX_SIZE_SMEM_PER_BLOCK << " | thread: " << kernel_size << '/' << MAX_NUM_THREADS_PER_BLOCK << " | register: " << num_register << '/' << MAX_NUM_REGISTERS_PER_BLOCK << std::endl;
        cout << "kernel_size: " << kernel_size << std::endl;
        cout << "n_channel_per_block: " << n_channel_per_block << std::endl;
        cout << "threads_per_block: " << kernel_size * n_channel_per_block << std::endl;
        cout << "blocks.x: " << (n_channel_per_block > 0? ((total_channel + n_channel_per_block - 1) / n_channel_per_block) : -1) << std::endl;
        cout << "blocks.y: " << n_corr << std::endl;
        cout << "shared_memory_size: " << shared_memory_size << std::endl;
        cout << "register_per_block: " << num_register * n_channel_per_block << std::endl;
    )
    shared_memory_size *= n_channel_per_block;
    AT_ASSERT(n_channel_per_block > 0, "shared_memory_size or kernel_size or num_reg exceeds limitation");

    auto grad_input = at::zeros_like(input);

    // 2*n_channel_per_block*bg_width*bg_height*sizeof(scalar_t)
    // should be less than or equal to 32768
    // when block_per_SM == 2 to maxmize occupancy

    const dim3 threads_per_block(kernel_size, n_channel_per_block);
    const dim3 blocks((total_channel + n_channel_per_block - 1) / n_channel_per_block, n_corr);

#define CALL_BACKWARD() \
    lacorr2d_backward_cuda_kernel<scalar_t><<<blocks, threads_per_block, shared_memory_size>>>( \
        input.data<scalar_t>(), \
        grad_output.data<scalar_t>(), \
        grad_input.data<scalar_t>(), \
        kernel_height, \
        kernel_width, \
        stride_height, \
        stride_width, \
        -padding_top, \
        -padding_left, \
        n_corr_h, \
        n_corr_w, \
        total_channel, \
        channel_size, \
        height, \
        width);

#if FLOAT_ONLY
    using scalar_t = float;
    CALL_BACKWARD()
#else
    AT_DISPATCH_FLOATING_TYPES(input.type(), "lacorr2d_backward_cuda", ([&] {
        CALL_BACKWARD()
    }));
#endif
    gpuErrchk(cudaPeekAtLastError());

    return {grad_input};
}