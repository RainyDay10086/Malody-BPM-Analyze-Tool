#if defined(__clang__)
#pragma clang fp contract(off)
#elif defined(__GNUC__)
#pragma GCC optimize("fp-contract=off")
#elif defined(_MSC_VER)
#pragma float_control(precise, on)
#pragma fp_contract(off)
#endif

#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <math.h>

#if defined(_WIN32)
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#endif

#if defined(__APPLE__)
#include <dispatch/dispatch.h>
#endif

#if defined(__x86_64__) || defined(_M_X64)
#include <xmmintrin.h>
#elif defined(_MSC_VER) && !defined(__clang__) && defined(_M_ARM64)
#include <intrin.h>
#endif

#ifdef _WIN32
#define EXPORT __declspec(dllexport)
#else
#define EXPORT __attribute__((visibility("default")))
#endif

// ============================================================================
// 1. 双二阶 IIR 级联滤波器 (Direct Form II)
//    - cascade_biquad_inplace_f32: 100% 还原 Malody 0x100191e84 单精度 float32 状态机
//      (含 0x100191e88 - 0x100191f3b 硬件级 FTZ Flush-To-Zero 次正规数清零标志)
//    - cascade_biquad_inplace:     增强模式双精度 float64 状态机 (消除长音频递归舍入累积)
// ============================================================================

static inline uint64_t enable_ftz_state(void) {
#if defined(__x86_64__) || defined(_M_X64)
    uint32_t old_mxcsr = _mm_getcsr();
    _mm_setcsr(old_mxcsr | 0x8000U);
    return (uint64_t)old_mxcsr;
#elif defined(__aarch64__)
    uint64_t old_fpcr;
    __asm__ __volatile__("mrs %0, fpcr" : "=r"(old_fpcr) : : "memory");
    uint64_t new_fpcr = old_fpcr | (1ULL << 24);
    __asm__ __volatile__("msr fpcr, %0" : : "r"(new_fpcr) : "memory");
    return old_fpcr;
#elif defined(_MSC_VER) && defined(_M_ARM64)
    /* ARM64_FPCR = _ARM64_SYSREG(3, 3, 4, 4, 0) = 0x5A20 */
    uint64_t old_fpcr = (uint64_t)_ReadStatusReg(0x5A20);
    uint64_t new_fpcr = old_fpcr | (1ULL << 24);
    _WriteStatusReg(0x5A20, (__int64)new_fpcr);
    return old_fpcr;
#else
    return 0;
#endif
}

static inline void restore_ftz_state(uint64_t old_state) {
#if defined(__x86_64__) || defined(_M_X64)
    _mm_setcsr((uint32_t)old_state);
#elif defined(__aarch64__)
    __asm__ __volatile__("msr fpcr, %0" : : "r"(old_state) : "memory");
#elif defined(_MSC_VER) && defined(_M_ARM64)
    _WriteStatusReg(0x5A20, (__int64)old_state);
#else
    (void)old_state;
#endif
}

EXPORT void biquad_filter_inplace_f32(float *data, int n, float b1_b0, float b2_b0, float a1, float a2, float gain) {
    float w1 = 0.0f;
    float w2 = 0.0f;
    for (int i = 0; i < n; i++) {
        float w0 = data[i] * gain - a1 * w1 - a2 * w2;
        float out = b1_b0 * w1 + w0 + b2_b0 * w2;
        data[i] = out;
        w2 = w1;
        w1 = w0;
    }
}

EXPORT void cascade_biquad_inplace_f32(float *data, int n, const double *stages, int num_stages) {
    uint64_t old_ftz = enable_ftz_state();
    for (int s = 0; s < num_stages; s++) {
        const double *st = stages + s * 5;
        biquad_filter_inplace_f32(
            data, n,
            (float)st[0], (float)st[1], (float)st[2], (float)st[3], (float)st[4]
        );
    }
    restore_ftz_state(old_ftz);
}

EXPORT void biquad_filter_inplace(float *data, int n, double b1_b0, double b2_b0, double a1, double a2, double gain) {
    double w1 = 0.0;
    double w2 = 0.0;
    for (int i = 0; i < n; i++) {
        double w0 = gain * (double)data[i] - a1 * w1 - a2 * w2;
        double out = w0 + b1_b0 * w1 + b2_b0 * w2;
        w2 = w1;
        w1 = w0;
        data[i] = (float)out;
    }
}

EXPORT void cascade_biquad_inplace(float *data, int n, const double *stages, int num_stages) {
    for (int s = 0; s < num_stages; s++) {
        const double *st = stages + s * 5;
        biquad_filter_inplace(data, n, st[0], st[1], st[2], st[3], st[4]);
    }
}

// ============================================================================
// 2. 4096-bin IEEE-754 浮点直方图自适应中值查找 (Malody 0x10017e3b2)
//    包络管线以 esi = -4 调用，由此推得: 移位量 = esi+0x17 = 19,
//    偏移 = 1<<(esi+0x16) = 0x40000, 桶数 = (1<<(8-esi))+1 = 4097。
// ============================================================================

EXPORT float compute_median_float_histogram(const float *data, int n) {
    if (n <= 0) return 0.0f;
    unsigned int hist[4097];
    memset(hist, 0, sizeof(hist));

    for (int i = 0; i < n; i++) {
        float val = data[i];
        unsigned int u;
        memcpy(&u, &val, 4);
        if ((int)u < 0) {
            u = 0;
        }
        u = (u + 0x40000) >> 19;
        if (u > 4096) u = 4096;
        hist[u]++;
    }

    int half = n / 2;
    int acc = 0;
    unsigned int median_bin = 0;
    for (unsigned int b = 0; b <= 4096; b++) {
        acc += hist[b];
        if (acc >= half) {
            median_bin = b;
            break;
        }
    }

    unsigned int res_u = median_bin << 19;
    float median_val;
    memcpy(&median_val, &res_u, 4);
    return median_val;
}

// ============================================================================
// 3. 频段能量对数动态压缩与群延迟时间对齐累加 (Malody 0x1000bf65b - 0x1000bf733 & 0x10017e48f)
//    利用 IEEE-754 浮点尾数/指数位减法快速逼近 log2:
//    (val.u - 0x3f800000) * (weight * 8.262958317573066e-08f)
//    注：Malody 0x10017e4e1 & 0x10017e4f4 对末端指针执行 `andq $-0x10` (4个float对齐)，
//    因此 exact 模式下调用方传入 n_aligned = n & ~3。
// ============================================================================

EXPORT void accumulate_band_log_novelty(
    const float *band_data,
    int n,
    int delay_samples,
    float weight,
    float median_val,
    float *master_env,
    int exact
) {
    int abs_delay = (delay_samples < 0) ? -delay_samples : delay_samples;
    if (n < abs_delay + 8) return;
    // exact 模式对齐 Malody 0x1000bf6df 的无条件 divss：median=0 时得到 inf；
    // enhanced 模式保留原来的有限值兜底，避免静音段把增强结果打成 NaN。
    float scale = (exact || median_val > 0.0f) ? (2.0f / median_val) : (2.0f / 1e-12f);
    float c_log = weight * 8.262958317573066e-08f;

    for (int i = 0; i < n; i++) {
        int target_idx = i + delay_samples;
        if (target_idx >= 0 && target_idx < n) {
            float v = band_data[i];
            union { float f; int32_t i; } u;
            u.f = 1.0f + v * scale;
            int32_t diff = u.i - 0x3f800000;
            float compressed = (float)diff * c_log;
            master_env[target_idx] += compressed;
        }
    }
}

// ============================================================================
// 4. 1000 Hz 毫秒级重采样 (Malody 0x100192871)
//    原版使用 double factor = 1000.0 / sample_rate, n_dst = ceil(n_src * factor),
//    idx = lrint((double)m / factor)
// ============================================================================

EXPORT void resample_to_1000hz(
    const float *src,
    int n_src,
    int sample_rate,
    float *dst,
    int n_dst
) {
    double factor = 1000.0 / (double)sample_rate;
    for (int m = 0; m < n_dst; m++) {
        int64_t idx = llrint((double)m / factor);
        if (idx >= 0 && idx < n_src) {
            dst[m] = src[idx];
        } else {
            dst[m] = 0.0f;
        }
    }
}

// ============================================================================
// 5. 步骤 8: 1000 Hz 零相位奇对称 IIR 差分滤波 (Malody 0x1000bf82e - 0x1000bf8e9)
//    Stage 15 参数: b1_b0 = 0, b2_b0 = 0, a1 = -1.4, a2 = 0.48, gain = 0.2
//    Malody 汇编精确错位差分索引 (彻底抵消 i 时刻中心冲击项，构造纯奇对称导数核):
//      dst[0]     =  A[n - 2]            (= bwd[1])
//      dst[i]     =  A[n - 2 - i] - B[i - 1] (= bwd[i + 1] - fwd[i - 1], 1 <= i <= n - 2)
//      dst[n - 1] = -B[n - 2]            (= -fwd[n - 2])
// ============================================================================

EXPORT void apply_zero_phase_diff_exact(const float *src, int n, float *dst, const double *stage15) {
    if (n <= 0) return;
    float *B = (float *)malloc((size_t)n * sizeof(float));
    float *A = (float *)malloc((size_t)n * sizeof(float));
    if (!B || !A) {
        free(B);
        free(A);
        return;
    }

    uint64_t old_ftz = enable_ftz_state();
    float b1 = (float)stage15[0], b2 = (float)stage15[1];
    float a1 = (float)stage15[2], a2 = (float)stage15[3], g = (float)stage15[4];
    float fw1 = 0.0f, fw2 = 0.0f;
    float bw1 = 0.0f, bw2 = 0.0f;

    for (int i = 0; i < n; i++) {
        float wf = src[i] * g - a1 * fw1 - a2 * fw2;
        B[i] = b1 * fw1 + wf + b2 * fw2;
        fw2 = fw1;
        fw1 = wf;

        float wb = src[n - 1 - i] * g - a1 * bw1 - a2 * bw2;
        A[i] = b1 * bw1 + wb + b2 * bw2;
        bw2 = bw1;
        bw1 = wb;
    }
    restore_ftz_state(old_ftz);

    if (n >= 2) {
        dst[n - 1] = -B[n - 2];
        for (int rdx = n - 1; rdx > 1; rdx--) {
            int k = (n - 1) - rdx;
            dst[rdx - 1] = A[k] - B[rdx - 2];
        }
        dst[0] = A[n - 2];
    } else {
        dst[0] = 0.0f;
    }

    free(B);
    free(A);
}

EXPORT void apply_zero_phase_diff(const float *src, int n, float *dst) {
    if (n <= 0) return;
    float *B = (float *)malloc((size_t)n * sizeof(float));
    float *A = (float *)malloc((size_t)n * sizeof(float));
    if (!B || !A) {
        free(B);
        free(A);
        return;
    }

    const double a1 = -1.4, a2 = 0.48, g = 0.2;
    double fw1 = 0.0, fw2 = 0.0;
    double bw1 = 0.0, bw2 = 0.0;

    for (int i = 0; i < n; i++) {
        double wf = g * (double)src[i] - a1 * fw1 - a2 * fw2;
        B[i] = (float)wf;
        fw2 = fw1;
        fw1 = wf;

        double wb = g * (double)src[n - 1 - i] - a1 * bw1 - a2 * bw2;
        A[i] = (float)wb;
        bw2 = bw1;
        bw1 = wb;
    }

    if (n >= 2) {
        dst[n - 1] = -B[n - 2];
        for (int rdx = n - 1; rdx > 1; rdx--) {
            int k = (n - 1) - rdx;
            dst[rdx - 1] = A[k] - B[rdx - 2];
        }
        dst[0] = A[n - 2];
    } else {
        dst[0] = 0.0f;
    }

    free(B);
    free(A);
}

// ============================================================================
// 6. 原版 PCM 多声道转单声道混音 (Malody 0x10017e57d - 0x10017e7b3)
//    - 16-bit PCM (FMT=2): 每声道乘 3.0517578125e-05f (1/32768) 累加，再乘 (1.0f / channels)
//    - 24-bit PCM (FMT=3): 每声道乘 1.1920928955078125e-07f (1/8388608) 累加，再乘 (1.0f / channels)
//    - 32-bit float PCM (FMT=5): 每声道直接 float32 累加，再乘 (1.0f / channels)
// ============================================================================

EXPORT void downmix_s16le_exact(const int16_t *src, int n_frames, int channels, float *dst) {
    const float scale = 3.0517578125e-05f;
    const float inv_ch = (channels >= 2) ? (1.0f / (float)channels) : 1.0f;
    for (int i = 0; i < n_frames; i++) {
        float sum = 0.0f;
        const int16_t *frame = src + (size_t)i * channels;
        for (int c = 0; c < channels; c++) {
            sum += (float)frame[c] * scale;
        }
        if (channels >= 2) {
            sum *= inv_ch;
        }
        dst[i] = sum;
    }
}

EXPORT void downmix_s24le_exact(const uint8_t *src, int n_frames, int channels, float *dst) {
    const float scale = 1.1920928955078125e-07f;
    const float inv_ch = (channels >= 2) ? (1.0f / (float)channels) : 1.0f;
    const size_t stride = (size_t)channels * 3;
    for (int i = 0; i < n_frames; i++) {
        float sum = 0.0f;
        const uint8_t *p = src + (size_t)i * stride;
        for (int c = 0; c < channels; c++) {
            uint32_t raw24 = (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16);
            int32_t sample = (raw24 & 0x800000U) ? (int32_t)(raw24 | 0xFF000000U) : (int32_t)raw24;
            sum += (float)sample * scale;
            p += 3;
        }
        if (channels >= 2) {
            sum *= inv_ch;
        }
        dst[i] = sum;
    }
}

EXPORT void downmix_f32le_exact(const float *src, int n_frames, int channels, float *dst) {
    const float inv_ch = (channels >= 2) ? (1.0f / (float)channels) : 1.0f;
    for (int i = 0; i < n_frames; i++) {
        float sum = 0.0f;
        const float *frame = src + (size_t)i * channels;
        for (int c = 0; c < channels; c++) {
            sum += frame[c];
        }
        if (channels >= 2) {
            sum *= inv_ch;
        }
        dst[i] = sum;
    }
}

// ============================================================================
// 7. 原版单精度 float32 基-2 FFT 与全局自相关 (Malody 0x1000344a6, 0x100034599, 0x100191f40)
//    - 预计算 2^20 (1048576) 点单精度旋转因子表: angle = i * (-5.992112452678286e-06)
//    - 20-bit 位反转表查表重排
//    - k=0 直接蝶形加减，k>=1 调用复数乘法并单精度蝶形更新
// ============================================================================

static float *g_twiddle_re = NULL;
static float *g_twiddle_im = NULL;
static uint32_t *g_bitrev_20 = NULL;

static void init_malody_fft_tables_impl(void) {
    float *tw_re = (float *)malloc(1048576 * sizeof(float));
    float *tw_im = (float *)malloc(1048576 * sizeof(float));
    uint32_t *bitrev = (uint32_t *)malloc(1048576 * sizeof(uint32_t));
    if (!tw_re || !tw_im || !bitrev) {
        free(tw_re);
        free(tw_im);
        free(bitrev);
        return;
    }

    // 0x1000344b6 - 0x1000344fb
    for (int i = 0; i < 1048576; i++) {
        double angle = (double)i * (-5.99211245267828600956290328759e-06);
        tw_re[i] = (float)cos(angle);
        tw_im[i] = (float)sin(angle);
    }

    // 0x1000344fd - 0x10003454d
    bitrev[0] = 0;
    uint32_t rdx = 1;
    uint32_t rax = 0x80000;
    while (1) {
        for (uint32_t i = 0; i < rdx; i++) {
            bitrev[i + rdx] = bitrev[i] + rax;
        }
        rdx += rdx;
        if (rdx > 0xfffff) break;
        rax >>= 1;
    }

    g_twiddle_im = tw_im;
    g_bitrev_20 = bitrev;
    g_twiddle_re = tw_re;
}

#if defined(__APPLE__)
static dispatch_once_t g_fft_once_token;
static void init_malody_fft_tables(void) {
    dispatch_once(&g_fft_once_token, ^{
        init_malody_fft_tables_impl();
    });
}
#elif defined(_WIN32)
static INIT_ONCE g_fft_once_win32 = INIT_ONCE_STATIC_INIT;
static BOOL CALLBACK init_malody_fft_tables_win32(PINIT_ONCE InitOnce, PVOID Parameter, PVOID *Context) {
    (void)InitOnce;
    (void)Parameter;
    (void)Context;
    init_malody_fft_tables_impl();
    return TRUE;
}
static void init_malody_fft_tables(void) {
    InitOnceExecuteOnce(&g_fft_once_win32, init_malody_fft_tables_win32, NULL, NULL);
}
#else
static void init_malody_fft_tables(void) {
    if (g_twiddle_re == NULL) {
        init_malody_fft_tables_impl();
    }
}
#endif

static void malody_fft_radix2_f32(int p, const float *in_re, const float *in_im, float *out_re, float *out_im) {
    init_malody_fft_tables();
    if (!g_twiddle_re || !g_twiddle_im || !g_bitrev_20) return;
    int n = 1 << p;
    int step = 1 << (20 - p);

    // 0x1000345e3 - 0x1000345f7: 位反转拷贝
    for (int i = 0; i < n; i++) {
        uint32_t rev_idx = g_bitrev_20[i * step];
        out_re[i] = in_re[rev_idx];
        out_im[i] = in_im[rev_idx];
    }

    if (p == 0) return;

    // 0x100034609 - 0x10003471f: 蝶形运算
    int twiddle_step = 0x80000;
    int half_m = 1;
    while (half_m < n) {
        int m = half_m * 2;
        for (int block = 0; block < n; block += m) {
            // k = 0: 无复数乘法直接加减 (0x10003466b - 0x100034694)
            int idx0 = block;
            int idx1 = block + half_m;
            float r0 = out_re[idx0], i0 = out_im[idx0];
            float r1 = out_re[idx1], i1 = out_im[idx1];
            out_re[idx1] = r0 - r1;
            out_im[idx1] = i0 - i1;
            out_re[idx0] = r0 + r1;
            out_im[idx0] = i0 + i1;

            // k = 1 .. half_m - 1 (0x1000346a4 - 0x1000346ea)
            int tw_idx = twiddle_step;
            for (int k = 1; k < half_m; k++) {
                int a = block + k;
                int b = a + half_m;
                float br = out_re[b], bi = out_im[b];
                float wr = g_twiddle_re[tw_idx], wi = g_twiddle_im[tw_idx];
                // 0x100192590 复数乘法
                float tr = br * wr - bi * wi;
                float ti = bi * wr + br * wi;
                float ar = out_re[a], ai = out_im[a];
                out_re[b] = ar - tr;
                out_im[b] = ai - ti;
                out_re[a] = ar + tr;
                out_im[a] = ai + ti;
                tw_idx += twiddle_step;
            }
        }
        twiddle_step >>= 1;
        half_m = m;
    }
}

EXPORT void autocorr_fft_exact_c(const float *diff_env, int n, int max_lag, float *acf_out) {
    if (n <= 1 || max_lag <= 0) return;

    // 0x100191f62 - 0x100191fb5: 原版对 n + max_lag - 2 取位长，
    // 等价于 p = ceil(log2(n + max_lag - 1))
    uint32_t target_len = (uint32_t)(n + max_lag - 1);
    int p = 0;
    while ((1U << p) < target_len) {
        p++;
    }
    if (p > 20) p = 20;
    int n_padded = 1 << p;

    float *in_re = (float *)calloc((size_t)n_padded, sizeof(float));
    float *in_im = (float *)calloc((size_t)n_padded, sizeof(float));
    float *out_re = (float *)calloc((size_t)n_padded, sizeof(float));
    float *out_im = (float *)calloc((size_t)n_padded, sizeof(float));
    if (!in_re || !in_im || !out_re || !out_im) {
        free(in_re); free(in_im); free(out_re); free(out_im);
        return;
    }

    int copy_n = (n < n_padded) ? n : n_padded;
    memcpy(in_re, diff_env, (size_t)copy_n * sizeof(float));

    // 正向 FFT (0x100192019)
    malody_fft_radix2_f32(p, in_re, in_im, out_re, out_im);

    // 功率谱密度 re^2 + im^2 (0x100192028 - 0x10019205a)
    for (int i = 0; i < n_padded; i++) {
        float r = out_re[i];
        float m = out_im[i];
        in_re[i] = r * r + m * m;
        in_im[i] = 0.0f;
    }

    // 逆向 FFT (由于功率谱为偶对称实序列，再次调用正向 FFT 等价于未归一化 IFFT，0x10019205f)
    malody_fft_radix2_f32(p, in_re, in_im, out_re, out_im);

    // 乘以 1.0f / (float)n_padded (0x100192099 - 0x1001920c2)
    float scale = 1.0f / (float)n_padded;
    for (int i = 0; i < max_lag; i++) {
        acf_out[i] = out_re[i] * scale;
    }

    free(in_re);
    free(in_im);
    free(out_re);
    free(out_im);
}

// ============================================================================
// 8. 自相关局部峰值检测与全曲线性回归加速 (Malody 0x1000bfc0e & 0x1000c0353)
// ============================================================================

EXPORT int find_acf_local_peaks_c(const float *acf, int max_lag_cands, int *peaks_out) {
    int count = 0;
    int limit = max_lag_cands - 16;
    for (int lag = 16; lag < limit; lag++) {
        float center = acf[lag];
        if (center > 0.0f) {
            int is_peak = 1;
            for (int d = 1; d <= 15; d++) {
                if (center < acf[lag - d] || center < acf[lag + d]) {
                    is_peak = 0;
                    break;
                }
            }
            if (is_peak) {
                peaks_out[count++] = lag;
            }
        }
    }
    return count;
}

EXPORT int track_full_song_exact_c(
    const float *acf,
    int max_lag,
    double start_tau,
    double *out_stats
) {
    double cur_tau = start_tau;
    int64_t target = llrint(cur_tau);
    double S_kk = 0.0;
    double S_kt = 0.0;
    double S_tt = 0.0;
    int n_matched = 0;
    double k = 1.0;

    while (target + 10 < max_lag) {
        int64_t base = target - 10;
        int64_t local_max_idx = base;
        float max_val = acf[base];
        for (int64_t idx = base + 1; idx <= target + 10; idx++) {
            if (acf[idx] > max_val) {
                max_val = acf[idx];
                local_max_idx = idx;
            }
        }
        if (max_val > 0.0f && llabs(local_max_idx - target) <= 8) {
            int64_t c = local_max_idx;
            n_matched++;
            double ym1 = (double)acf[c - 1];
            double y0_d = (double)acf[c];
            double yp1 = (double)acf[c + 1];
            double denom = (y0_d - ym1) + (y0_d - yp1);
            double delta = 0.5 * (yp1 - ym1) / denom;
            double exact_t = (double)c + delta;
            if (!isfinite(exact_t)) {
                // 原版 denom=0 时会得到 NaN/inf 并在后续 lrint 中越界；
                // 这里按失败熔断，保持可观察结果一致并避免未定义行为。
                out_stats[0] = 0.0;
                out_stats[1] = 0.0;
                out_stats[2] = 999.0;
                out_stats[3] = 1.0;
                return 0;
            }
            S_kk += k * k;
            S_kt += k * exact_t;
            S_tt += exact_t * exact_t;
            cur_tau = S_kt / S_kk;
            target = llrint(exact_t + cur_tau);
        } else {
            target = llrint((double)target + cur_tau);
            break;
        }
        k += 1.0;
    }

    if (n_matched < 4) {
        out_stats[0] = 0.0;
        out_stats[1] = 0.0;
        out_stats[2] = 999.0;
        out_stats[3] = 1.0;
        return n_matched;
    }

    double raw_bpm = 60000.0 * S_kk / S_kt;
    // Malody 0x1000c0565-0x1000c057d: 先乘 S_kk*S_tt，再连续除以两次 S_kt，
    // 不能改写成 /(S_kt*S_kt)，否则 double 末位不同。
    double term = (((S_kk * S_tt) / S_kt) / S_kt - 1.0) / ((double)n_matched - 1.0) * (double)n_matched;
    double raw_instab = sqrt(term) * raw_bpm;
    double var_t = (S_tt - (S_kt * S_kt) / S_kk) / ((double)n_matched - 1.0);
    double sigma_t = sqrt(var_t);
    double rel_inst = raw_instab / raw_bpm;

    out_stats[0] = raw_bpm;
    out_stats[1] = raw_instab;
    out_stats[2] = sigma_t;
    out_stats[3] = rel_inst;
    return n_matched;
}

EXPORT int track_full_song_relaxed_c(
    const float *acf,
    int max_lag,
    double start_tau,
    int consec_miss_limit,
    int tol,
    int min_beats,
    double *out_stats
) {
    double cur_tau = start_tau;
    int64_t target = llrint(cur_tau);
    double S_kk = 0.0;
    double S_kt = 0.0;
    double S_tt = 0.0;
    int n_matched = 0;
    int consec_miss = 0;
    double k = 1.0;

    while (target + 10 < max_lag && target - 10 >= 0) {
        int64_t base = target - 10;
        int64_t local_max_idx = base;
        float max_val = acf[base];
        for (int64_t idx = base + 1; idx <= target + 10; idx++) {
            if (acf[idx] > max_val) {
                max_val = acf[idx];
                local_max_idx = idx;
            }
        }
        if (max_val > 0.0f && llabs(local_max_idx - target) <= tol) {
            int64_t c = local_max_idx;
            n_matched++;
            consec_miss = 0;
            double ym1 = (double)acf[c - 1];
            double y0_d = (double)acf[c];
            double yp1 = (double)acf[c + 1];
            double denom = (y0_d - ym1) + (y0_d - yp1);
            double delta = (fabs(denom) > 1e-12) ? (0.5 * (yp1 - ym1) / denom) : 0.0;
            double exact_t = (double)c + delta;
            S_kk += k * k;
            S_kt += k * exact_t;
            S_tt += exact_t * exact_t;
            cur_tau = S_kt / S_kk;
            target = llrint(exact_t + cur_tau);
        } else {
            consec_miss++;
            if (consec_miss > consec_miss_limit) {
                break;
            }
            target = llrint((double)target + cur_tau);
        }
        k += 1.0;
    }

    if (n_matched < min_beats || S_kt <= 0.0) {
        out_stats[0] = 0.0;
        out_stats[1] = 0.0;
        out_stats[2] = 999.0;
        out_stats[3] = 1.0;
        return n_matched;
    }

    double raw_bpm = 60000.0 * S_kk / S_kt;
    double term = ((S_kk * S_tt) / (S_kt * S_kt) - 1.0) / ((double)n_matched - 1.0) * (double)n_matched;
    double raw_instab = sqrt(term > 0.0 ? term : 0.0) * raw_bpm;
    double var_t = (S_tt - (S_kt * S_kt) / S_kk) / ((double)n_matched - 1.0);
    double sigma_t = sqrt(var_t > 0.0 ? var_t : 0.0);
    double rel_inst = raw_instab / raw_bpm;

    out_stats[0] = raw_bpm;
    out_stats[1] = raw_instab;
    out_stats[2] = sigma_t;
    out_stats[3] = rel_inst;
    return n_matched;
}

EXPORT int scan_full_song_harmonics_c(
    const float *acf,
    int max_lag,
    double tau,
    int win_radius,
    int tol,
    int min_hits,
    double min_coverage,
    double *out_stats
) {
    if (tau <= 0.0 || max_lag <= 0) return 0;
    int max_k = (int)((double)(max_lag - win_radius - 1) / tau);
    if (max_k > 5000) max_k = 5000;
    if (max_k < 4) return 0;

    double S_kk = 0.0;
    double S_kt = 0.0;
    double S_tt = 0.0;
    int n_hits = 0;

    for (int k = 1; k <= max_k; k++) {
        int64_t target = llrint((double)k * tau);
        if (target + win_radius >= max_lag || target - win_radius < 0) break;
        int64_t base = target - win_radius;
        int64_t local_max_idx = base;
        float max_val = acf[base];
        for (int64_t idx = base + 1; idx <= target + win_radius; idx++) {
            if (acf[idx] > max_val) {
                max_val = acf[idx];
                local_max_idx = idx;
            }
        }
        if (max_val > 0.0f && llabs(local_max_idx - target) <= tol) {
            int64_t c = local_max_idx;
            double ym1 = (double)acf[c - 1];
            double y0_d = (double)acf[c];
            double yp1 = (double)acf[c + 1];
            double denom = (y0_d - ym1) + (y0_d - yp1);
            double delta = (fabs(denom) > 1e-12) ? (0.5 * (yp1 - ym1) / denom) : 0.0;
            double exact_t = (double)c + delta;
            double kd = (double)k;
            S_kk += kd * kd;
            S_kt += kd * exact_t;
            S_tt += exact_t * exact_t;
            n_hits++;
        }
    }

    if (n_hits >= min_hits && S_kt > 0.0 && S_kk > 0.0) {
        double tau_ref = S_kt / S_kk;
        if (fabs(tau_ref - tau) >= 0.005 && fabs(tau_ref - tau) <= (double)tol) {
            int max_k2 = (int)((double)(max_lag - win_radius - 1) / tau_ref);
            if (max_k2 > 5000) max_k2 = 5000;
            double S_kk2 = 0.0, S_kt2 = 0.0, S_tt2 = 0.0;
            int n_hits2 = 0;
            for (int k = 1; k <= max_k2; k++) {
                int64_t target = llrint((double)k * tau_ref);
                if (target + win_radius >= max_lag || target - win_radius < 0) break;
                int64_t base = target - win_radius;
                int64_t local_max_idx = base;
                float max_val = acf[base];
                for (int64_t idx = base + 1; idx <= target + win_radius; idx++) {
                    if (acf[idx] > max_val) {
                        max_val = acf[idx];
                        local_max_idx = idx;
                    }
                }
                if (max_val > 0.0f && llabs(local_max_idx - target) <= tol) {
                    int64_t c = local_max_idx;
                    double ym1 = (double)acf[c - 1];
                    double y0_d = (double)acf[c];
                    double yp1 = (double)acf[c + 1];
                    double denom = (y0_d - ym1) + (y0_d - yp1);
                    double delta = (fabs(denom) > 1e-12) ? (0.5 * (yp1 - ym1) / denom) : 0.0;
                    double exact_t = (double)c + delta;
                    double kd = (double)k;
                    S_kk2 += kd * kd;
                    S_kt2 += kd * exact_t;
                    S_tt2 += exact_t * exact_t;
                    n_hits2++;
                }
            }
            if (n_hits2 >= n_hits) {
                S_kk = S_kk2;
                S_kt = S_kt2;
                S_tt = S_tt2;
                n_hits = n_hits2;
                max_k = max_k2;
            }
        }
    }

    double coverage = (max_k > 0) ? ((double)n_hits / (double)max_k) : 0.0;
    if (n_hits < min_hits || coverage < min_coverage || S_kt <= 0.0) {
        if (out_stats) {
            out_stats[0] = 0.0;
            out_stats[1] = 0.0;
            out_stats[2] = 999.0;
            out_stats[3] = 1.0;
            out_stats[4] = coverage;
            out_stats[5] = 0.0;
        }
        return n_hits;
    }

    double raw_bpm = 60000.0 * S_kk / S_kt;
    double term = ((S_kk * S_tt) / (S_kt * S_kt) - 1.0) / ((double)n_hits - 1.0) * (double)n_hits;
    double raw_instab = sqrt(term > 0.0 ? term : 0.0) * raw_bpm;
    double var_t = (S_tt - (S_kt * S_kt) / S_kk) / ((double)n_hits - 1.0);
    double sigma_t = sqrt(var_t > 0.0 ? var_t : 0.0);
    double rel_inst = raw_instab / raw_bpm;
    double score = (double)n_hits * coverage / (1.0 + rel_inst * 10.0);

    if (out_stats) {
        out_stats[0] = raw_bpm;
        out_stats[1] = raw_instab;
        out_stats[2] = sigma_t;
        out_stats[3] = rel_inst;
        out_stats[4] = coverage;
        out_stats[5] = score;
    }
    return n_hits;
}

// ============================================================================
// 8.5. 增强优化模式 2.0 专用能量加权梳状扫描与批量多核并发
// ============================================================================

static inline int scan_comb_single_pass_v2(
    const float *acf,
    int max_lag,
    double cur_tau,
    int win_radius,
    int tol,
    int max_k,
    double *out_S_kk,
    double *out_S_kt,
    double *out_S_tt,
    double *out_sum_amp
) {
    double S_kk = 0.0, S_kt = 0.0, S_tt = 0.0, sum_amp = 0.0;
    int n_hits = 0;
    for (int k = 1; k <= max_k; k++) {
        int64_t target = llrint((double)k * cur_tau);
        if (target + win_radius >= max_lag || target - win_radius < 0) break;
        int64_t base = target - win_radius;
        int64_t local_max_idx = base;
        float max_val = acf[base];
        for (int64_t idx = base + 1; idx <= target + win_radius; idx++) {
            if (acf[idx] > max_val) {
                max_val = acf[idx];
                local_max_idx = idx;
            }
        }
        if (max_val > 0.0f && llabs(local_max_idx - target) <= tol) {
            int64_t c = local_max_idx;
            // P0 优化: 严格非边界局部凸峰 (Strict Concave Peak) 校验
            if (c > base && c < target + win_radius && c > 0 && c + 1 < max_lag) {
                double ym1 = (double)acf[c - 1];
                double y0_d = (double)acf[c];
                double yp1 = (double)acf[c + 1];
                double denom = (y0_d - ym1) + (y0_d - yp1);
                // 确保严格极大值且向下凹 (denom > 1e-12)
                if (denom > 1e-12 && y0_d >= ym1 && y0_d >= yp1) {
                    double delta = 0.5 * (yp1 - ym1) / denom;
                    if (fabs(delta) <= 1.0) {
                        double exact_t = (double)c + delta;
                        double kd = (double)k;
                        S_kk += kd * kd;
                        S_kt += kd * exact_t;
                        S_tt += exact_t * exact_t;
                        sum_amp += y0_d;
                        n_hits++;
                    }
                }
            }
        }
    }
    *out_S_kk = S_kk;
    *out_S_kt = S_kt;
    *out_S_tt = S_tt;
    *out_sum_amp = sum_amp;
    return n_hits;
}

EXPORT int scan_full_song_harmonics_v2_c(
    const float *acf,
    int max_lag,
    double tau,
    int win_radius,
    int tol,
    int min_hits,
    double min_coverage,
    double *out_stats
) {
    if (tau <= 0.0 || max_lag <= 0) return 0;
    int max_k = (int)((double)(max_lag - win_radius - 1) / tau);
    if (max_k > 5000) max_k = 5000;
    if (max_k < 4) return 0;

    double S_kk = 0.0, S_kt = 0.0, S_tt = 0.0, sum_amp = 0.0;
    int n_hits = scan_comb_single_pass_v2(
        acf, max_lag, tau, win_radius, tol, max_k,
        &S_kk, &S_kt, &S_tt, &sum_amp
    );

    // P0 优化: 自适应多趟迭代梳状收敛 (最多 4 次迭代收敛循环)
    double cur_tau = tau;
    for (int iter = 0; iter < 4; iter++) {
        if (n_hits < min_hits || S_kt <= 0.0 || S_kk <= 0.0) break;
        double tau_next = S_kt / S_kk;
        // 已充分收敛 (< 1e-4) 则退出
        if (fabs(tau_next - cur_tau) < 1e-4) break;
        // 偏离原周期过多 (防发散) 则退出
        if (fabs(tau_next - tau) > (double)tol) break;

        int max_k_iter = (int)((double)(max_lag - win_radius - 1) / tau_next);
        if (max_k_iter > 5000) max_k_iter = 5000;
        if (max_k_iter < 4) break;

        double S_kk_iter, S_kt_iter, S_tt_iter, sum_amp_iter;
        int n_hits_iter = scan_comb_single_pass_v2(
            acf, max_lag, tau_next, win_radius, tol, max_k_iter,
            &S_kk_iter, &S_kt_iter, &S_tt_iter, &sum_amp_iter
        );

        if (n_hits_iter >= n_hits) {
            S_kk = S_kk_iter;
            S_kt = S_kt_iter;
            S_tt = S_tt_iter;
            sum_amp = sum_amp_iter;
            n_hits = n_hits_iter;
            max_k = max_k_iter;
            cur_tau = tau_next;
        } else {
            break;
        }
    }

    double coverage = (max_k > 0) ? ((double)n_hits / (double)max_k) : 0.0;
    if (n_hits < min_hits || coverage < min_coverage || S_kt <= 0.0) {
        if (out_stats) {
            out_stats[0] = 0.0;
            out_stats[1] = 0.0;
            out_stats[2] = 999.0;
            out_stats[3] = 1.0;
            out_stats[4] = coverage;
            out_stats[5] = 0.0;
            out_stats[6] = 0.0;
        }
        return n_hits;
    }

    double raw_bpm = 60000.0 * S_kk / S_kt;
    double term = ((S_kk * S_tt) / (S_kt * S_kt) - 1.0) / ((double)n_hits - 1.0) * (double)n_hits;
    double raw_instab = sqrt(term > 0.0 ? term : 0.0) * raw_bpm;
    double var_t = (S_tt - (S_kt * S_kt) / S_kk) / ((double)n_hits - 1.0);
    double sigma_t = sqrt(var_t > 0.0 ? var_t : 0.0);
    double rel_inst = raw_instab / raw_bpm;
    double mean_amp = (n_hits > 0) ? (sum_amp / (double)n_hits) : 0.0;
    
    // 综合加权能量评分: (sum_amp * coverage)
    double score = sum_amp * coverage / (1.0 + rel_inst * 10.0);

    if (out_stats) {
        out_stats[0] = raw_bpm;
        out_stats[1] = raw_instab;
        out_stats[2] = sigma_t;
        out_stats[3] = rel_inst;
        out_stats[4] = coverage;
        out_stats[5] = score;
        out_stats[6] = mean_amp;
    }
    return n_hits;
}

EXPORT void scan_batch_harmonics_v2_c(
    const float *acf,
    int max_lag,
    const double *taus,
    int num_taus,
    int win_radius,
    int tol,
    int min_hits,
    double min_coverage,
    double *out_stats_matrix,
    int *out_hits
) {
#if defined(__APPLE__)
    dispatch_apply((size_t)num_taus, dispatch_get_global_queue(DISPATCH_QUEUE_PRIORITY_DEFAULT, 0), ^(size_t i) {
        out_hits[i] = scan_full_song_harmonics_v2_c(
            acf, max_lag, taus[i], win_radius, tol, min_hits, min_coverage,
            out_stats_matrix + i * 7
        );
    });
#else
    for (int i = 0; i < num_taus; i++) {
        out_hits[i] = scan_full_song_harmonics_v2_c(
            acf, max_lag, taus[i], win_radius, tol, min_hits, min_coverage,
            out_stats_matrix + i * 7
        );
    }
#endif
}

// ============================================================================
// 9. 多核并行包络提取 (频段滤波/平方/中值；主包络仍按 b=0..3 顺序累加)
// ============================================================================

static float compute_band_single_pass(
    const float *pcm,
    float *buf,
    int n_samples,
    const double *st,
    int n_st,
    const double *sm_st,
    int has_sm,
    int exact
) {
    unsigned int hist[4097];
    memset(hist, 0, sizeof(hist));

    if (exact) {
        memcpy(buf, pcm, (size_t)n_samples * sizeof(float));

        // Malody 0x100191e84: FTZ 只在 biquad 级联内部生效。
        // 平方与直方图必须在默认 MXCSR 下执行；Band 0 的可选能量平滑仍是 biquad，需要 FTZ。
        uint64_t old_ftz = enable_ftz_state();
        for (int s = 0; s < n_st; s++) {
            biquad_filter_inplace_f32(
                buf, n_samples,
                (float)st[s * 5 + 0],
                (float)st[s * 5 + 1],
                (float)st[s * 5 + 2],
                (float)st[s * 5 + 3],
                (float)st[s * 5 + 4]
            );
        }
        restore_ftz_state(old_ftz);

        for (int i = 0; i < n_samples; i++) {
            buf[i] = buf[i] * buf[i];
        }

        if (has_sm) {
            uint64_t old_ftz_sm = enable_ftz_state();
            biquad_filter_inplace_f32(
                buf, n_samples,
                (float)sm_st[0],
                (float)sm_st[1],
                (float)sm_st[2],
                (float)sm_st[3],
                (float)sm_st[4]
            );
            restore_ftz_state(old_ftz_sm);
        }

        for (int i = 0; i < n_samples; i++) {
            unsigned int u;
            memcpy(&u, &buf[i], 4);
            if ((int)u < 0) u = 0;
            u = (u + 0x40000) >> 19;
            if (u > 4096) u = 4096;
            hist[u]++;
        }
    } else {
        double b1[4], b2[4], a1[4], a2[4], g[4], w1[4] = {0}, w2[4] = {0};
        for (int s = 0; s < n_st; s++) {
            b1[s] = st[s * 5 + 0];
            b2[s] = st[s * 5 + 1];
            a1[s] = st[s * 5 + 2];
            a2[s] = st[s * 5 + 3];
            g[s]  = st[s * 5 + 4];
        }
        double sb1 = sm_st[0], sb2 = sm_st[1], sa1 = sm_st[2], sa2 = sm_st[3], sg = sm_st[4];
        double sw1 = 0.0, sw2 = 0.0;

        for (int i = 0; i < n_samples; i++) {
            float x = pcm[i];
            for (int s = 0; s < n_st; s++) {
                double w0 = g[s] * (double)x - a1[s] * w1[s] - a2[s] * w2[s];
                double out = w0 + b1[s] * w1[s] + b2[s] * w2[s];
                w2[s] = w1[s]; w1[s] = w0;
                x = (float)out;
            }
            x = x * x;
            if (has_sm) {
                double w0 = sg * (double)x - sa1 * sw1 - sa2 * sw2;
                double out = w0 + sb1 * sw1 + sb2 * sw2;
                sw2 = sw1; sw1 = w0;
                x = (float)out;
            }
            buf[i] = x;
            unsigned int u;
            memcpy(&u, &x, 4);
            if ((int)u < 0) u = 0;
            u = (u + 0x40000) >> 19;
            if (u > 4096) u = 4096;
            hist[u]++;
        }
    }

    int half = n_samples / 2;
    int acc = 0;
    unsigned int median_bin = 0;
    for (unsigned int b = 0; b <= 4096; b++) {
        acc += hist[b];
        if (acc >= half) {
            median_bin = b;
            break;
        }
    }
    unsigned int res_u = median_bin << 19;
    float median_val;
    memcpy(&median_val, &res_u, 4);
    return median_val;
}

EXPORT void extract_onset_envelope_c(
    const float *pcm,
    int n_samples,
    int sample_rate,
    const double *all_stages,
    int exact,
    float *diff_1000hz,
    float *env_1000hz,
    int n_dst
) {
    if (n_samples <= 0 || n_dst <= 0) return;

    static const int band_start[4] = {0, 2, 6, 10};
    static const int band_count[4] = {2, 4, 4, 2};
    static const int sm_start[4]   = {12, 0, 0, 0};
    static const int sm_count[4]   = {1, 0, 0, 0};
    static const int delays[4]     = {-320, -64, -32, 0};
    static const float weights[4]  = {0.30f, 0.20f, 0.20f, 0.30f};

    float *master_env = (float *)calloc((size_t)n_samples, sizeof(float));
    float *band_bufs[4] = {NULL, NULL, NULL, NULL};
    float medians[4] = {0.0f, 0.0f, 0.0f, 0.0f};

    for (int b = 0; b < 4; b++) {
        band_bufs[b] = (float *)malloc((size_t)n_samples * sizeof(float));
        if (!band_bufs[b] || !master_env) {
            for (int k = 0; k <= b; k++) free(band_bufs[k]);
            free(master_env);
            return;
        }
    }

#ifdef __APPLE__
    float * const *bufs_ptr = band_bufs;
    float *med_ptr = medians;
    dispatch_apply(4, dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^(size_t b) {
        med_ptr[b] = compute_band_single_pass(
            pcm, bufs_ptr[b], n_samples,
            all_stages + band_start[b] * 5, band_count[b],
            all_stages + sm_start[b] * 5, sm_count[b],
            exact
        );
    });
#else
    for (int b = 0; b < 4; b++) {
        medians[b] = compute_band_single_pass(
            pcm, band_bufs[b], n_samples,
            all_stages + band_start[b] * 5, band_count[b],
            all_stages + sm_start[b] * 5, sm_count[b],
            exact
        );
    }
#endif

    // 按 b = 0, 1, 2, 3 严格顺序累加；主包络末端 2 级双二阶平滑 (Stages 13-14) 独立执行
    int n_aligned = exact ? (n_samples & ~3) : n_samples;
    const double *mst_st = all_stages + 13 * 5;

    if (n_aligned >= 328) {
        // exact: 无条件 2/median（median=0 时 inf），对齐 0x1000bf6df；
        // enhanced: 保留有限值兜底。
        float sc0 = exact ? (2.0f / medians[0]) : ((medians[0] > 0.0f) ? (2.0f / medians[0]) : (2.0f / 1e-12f));
        float sc1 = exact ? (2.0f / medians[1]) : ((medians[1] > 0.0f) ? (2.0f / medians[1]) : (2.0f / 1e-12f));
        float sc2 = exact ? (2.0f / medians[2]) : ((medians[2] > 0.0f) ? (2.0f / medians[2]) : (2.0f / 1e-12f));
        float sc3 = exact ? (2.0f / medians[3]) : ((medians[3] > 0.0f) ? (2.0f / medians[3]) : (2.0f / 1e-12f));
        float cl0 = weights[0] * 8.262958317573066e-08f;
        float cl1 = weights[1] * 8.262958317573066e-08f;
        float cl2 = weights[2] * 8.262958317573066e-08f;
        float cl3 = weights[3] * 8.262958317573066e-08f;
        const float *b0 = band_bufs[0], *b1 = band_bufs[1], *b2 = band_bufs[2], *b3 = band_bufs[3];

        if (exact) {
            // Malody 0x1000bf65b-0x1000bf733: 四个频段的对数贡献在默认 MXCSR 下
            // 按 b=0,1,2,3 顺序累加，FTZ 不在这一步生效。
            for (int t = 0; t < n_samples; t++) {
                float sum = 0.0f;
                if (t + 320 < n_aligned) {
                    union { float f; int32_t i; } u; u.f = 1.0f + b0[t + 320] * sc0;
                    sum += (float)(u.i - 0x3f800000) * cl0;
                }
                if (t + 64 < n_aligned) {
                    union { float f; int32_t i; } u; u.f = 1.0f + b1[t + 64] * sc1;
                    sum += (float)(u.i - 0x3f800000) * cl1;
                }
                if (t + 32 < n_aligned) {
                    union { float f; int32_t i; } u; u.f = 1.0f + b2[t + 32] * sc2;
                    sum += (float)(u.i - 0x3f800000) * cl2;
                }
                if (t < n_aligned) {
                    union { float f; int32_t i; } u; u.f = 1.0f + b3[t] * sc3;
                    sum += (float)(u.i - 0x3f800000) * cl3;
                }
                master_env[t] = sum;
            }
            // Malody 0x1000bf7a1: Stages 13-14 末端平滑走 biquad 级联，
            // FTZ 由 cascade_biquad_inplace_f32 内部启用/恢复。
            cascade_biquad_inplace_f32(master_env, n_samples, mst_st, 2);
        } else {
            double mb1_0 = mst_st[0], mb2_0 = mst_st[1], ma1_0 = mst_st[2], ma2_0 = mst_st[3], mg_0 = mst_st[4];
            double mb1_1 = mst_st[5], mb2_1 = mst_st[6], ma1_1 = mst_st[7], ma2_1 = mst_st[8], mg_1 = mst_st[9];
            double mw1_0 = 0.0, mw2_0 = 0.0, mw1_1 = 0.0, mw2_1 = 0.0;

            for (int t = 0; t < n_samples; t++) {
                float sum = 0.0f;
                if (t + 320 < n_aligned) {
                    union { float f; int32_t i; } u; u.f = 1.0f + b0[t + 320] * sc0;
                    sum += (float)(u.i - 0x3f800000) * cl0;
                }
                if (t + 64 < n_aligned) {
                    union { float f; int32_t i; } u; u.f = 1.0f + b1[t + 64] * sc1;
                    sum += (float)(u.i - 0x3f800000) * cl1;
                }
                if (t + 32 < n_aligned) {
                    union { float f; int32_t i; } u; u.f = 1.0f + b2[t + 32] * sc2;
                    sum += (float)(u.i - 0x3f800000) * cl2;
                }
                if (t < n_aligned) {
                    union { float f; int32_t i; } u; u.f = 1.0f + b3[t] * sc3;
                    sum += (float)(u.i - 0x3f800000) * cl3;
                }
                double w0 = mg_0 * (double)sum - ma1_0 * mw1_0 - ma2_0 * mw2_0;
                double out0 = w0 + mb1_0 * mw1_0 + mb2_0 * mw2_0;
                mw2_0 = mw1_0; mw1_0 = w0;
                float x = (float)out0;
                w0 = mg_1 * (double)x - ma1_1 * mw1_1 - ma2_1 * mw2_1;
                double out1 = w0 + mb1_1 * mw1_1 + mb2_1 * mw2_1;
                mw2_1 = mw1_1; mw1_1 = w0;
                master_env[t] = (float)out1;
            }
        }
        for (int b = 0; b < 4; b++) free(band_bufs[b]);
    } else {
        for (int b = 0; b < 4; b++) {
            accumulate_band_log_novelty(band_bufs[b], n_aligned, delays[b], weights[b], medians[b], master_env, exact);
            free(band_bufs[b]);
        }
        if (exact) {
            cascade_biquad_inplace_f32(master_env, n_samples, mst_st, 2);
        } else {
            cascade_biquad_inplace(master_env, n_samples, mst_st, 2);
        }
    }

    // 1000 Hz 重采样
    resample_to_1000hz(master_env, n_samples, sample_rate, env_1000hz, n_dst);
    free(master_env);

    // 零相位奇对称 IIR 错位差分 (Stage 15)
    if (exact) {
        apply_zero_phase_diff_exact(env_1000hz, n_dst, diff_1000hz, all_stages + 15 * 5);
    } else {
        apply_zero_phase_diff(env_1000hz, n_dst, diff_1000hz);
    }
}

// ============================================================================
// 11. 增强模式多声道频段平方能量域融合包络提取
//     对交错多声道浮点信号逐声道执行双精度 Biquad 子频段滤波，求跨声道平方能量均值:
//       E_b[i] = (sum_{c=0}^{C-1} (y_{c,b}[i])^2) * (1.0f / C)
//     随后统一执行能量平滑 (Band 0)、直方图中值去噪、对数压缩、重采样与零相位差分。
// ============================================================================

static float compute_band_multi_pass(
    const float *pcm,
    float *buf,
    int n_frames,
    int n_channels,
    const double *st,
    int n_st,
    const double *sm_st,
    int has_sm
) {
    memset(buf, 0, (size_t)n_frames * sizeof(float));

    double b1[4], b2[4], a1[4], a2[4], g[4];
    for (int s = 0; s < n_st; s++) {
        b1[s] = st[s * 5 + 0];
        b2[s] = st[s * 5 + 1];
        a1[s] = st[s * 5 + 2];
        a2[s] = st[s * 5 + 3];
        g[s]  = st[s * 5 + 4];
    }

    const size_t stride = (size_t)n_channels;
    for (int c = 0; c < n_channels; c++) {
        double w1[4] = {0.0, 0.0, 0.0, 0.0};
        double w2[4] = {0.0, 0.0, 0.0, 0.0};
        const float *ch_ptr = pcm + (size_t)c;
        for (int i = 0; i < n_frames; i++) {
            float x = ch_ptr[(size_t)i * stride];
            for (int s = 0; s < n_st; s++) {
                double w0 = g[s] * (double)x - a1[s] * w1[s] - a2[s] * w2[s];
                double out = w0 + b1[s] * w1[s] + b2[s] * w2[s];
                w2[s] = w1[s];
                w1[s] = w0;
                x = (float)out;
            }
            buf[i] += x * x;
        }
    }

    if (n_channels >= 2) {
        const float inv_ch = 1.0f / (float)n_channels;
        for (int i = 0; i < n_frames; i++) {
            buf[i] *= inv_ch;
        }
    }

    if (has_sm) {
        biquad_filter_inplace(buf, n_frames, sm_st[0], sm_st[1], sm_st[2], sm_st[3], sm_st[4]);
    }

    return compute_median_float_histogram(buf, n_frames);
}

EXPORT void extract_onset_envelope_multi_c(
    const float *pcm,
    int n_frames,
    int n_channels,
    int sample_rate,
    const double *all_stages,
    float *diff_1000hz,
    float *env_1000hz,
    int n_dst
) {
    if (n_frames <= 0 || n_channels <= 0 || n_dst <= 0) return;
    if (n_channels == 1) {
        extract_onset_envelope_c(
            pcm, n_frames, sample_rate, all_stages, 0, diff_1000hz, env_1000hz, n_dst
        );
        return;
    }

    static const int band_start[4] = {0, 2, 6, 10};
    static const int band_count[4] = {2, 4, 4, 2};
    static const int sm_start[4]   = {12, 0, 0, 0};
    static const int sm_count[4]   = {1, 0, 0, 0};
    static const int delays[4]     = {-320, -64, -32, 0};
    static const float weights[4]  = {0.30f, 0.20f, 0.20f, 0.30f};

    float *master_env = (float *)calloc((size_t)n_frames, sizeof(float));
    float *band_bufs[4] = {NULL, NULL, NULL, NULL};
    float medians[4] = {0.0f, 0.0f, 0.0f, 0.0f};

    for (int b = 0; b < 4; b++) {
        band_bufs[b] = (float *)malloc((size_t)n_frames * sizeof(float));
        if (!band_bufs[b] || !master_env) {
            for (int k = 0; k <= b; k++) free(band_bufs[k]);
            free(master_env);
            return;
        }
    }

#ifdef __APPLE__
    float * const *bufs_ptr = band_bufs;
    float *med_ptr = medians;
    dispatch_apply(4, dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^(size_t b) {
        med_ptr[b] = compute_band_multi_pass(
            pcm, bufs_ptr[b], n_frames, n_channels,
            all_stages + band_start[b] * 5, band_count[b],
            all_stages + sm_start[b] * 5, sm_count[b]
        );
    });
#else
    for (int b = 0; b < 4; b++) {
        medians[b] = compute_band_multi_pass(
            pcm, band_bufs[b], n_frames, n_channels,
            all_stages + band_start[b] * 5, band_count[b],
            all_stages + sm_start[b] * 5, sm_count[b]
        );
    }
#endif

    const double *mst_st = all_stages + 13 * 5;
    if (n_frames >= 328) {
        float sc0 = (medians[0] > 0.0f) ? (2.0f / medians[0]) : (2.0f / 1e-12f);
        float sc1 = (medians[1] > 0.0f) ? (2.0f / medians[1]) : (2.0f / 1e-12f);
        float sc2 = (medians[2] > 0.0f) ? (2.0f / medians[2]) : (2.0f / 1e-12f);
        float sc3 = (medians[3] > 0.0f) ? (2.0f / medians[3]) : (2.0f / 1e-12f);
        float cl0 = weights[0] * 8.262958317573066e-08f;
        float cl1 = weights[1] * 8.262958317573066e-08f;
        float cl2 = weights[2] * 8.262958317573066e-08f;
        float cl3 = weights[3] * 8.262958317573066e-08f;
        const float *b0 = band_bufs[0], *b1 = band_bufs[1], *b2 = band_bufs[2], *b3 = band_bufs[3];

        double mb1_0 = mst_st[0], mb2_0 = mst_st[1], ma1_0 = mst_st[2], ma2_0 = mst_st[3], mg_0 = mst_st[4];
        double mb1_1 = mst_st[5], mb2_1 = mst_st[6], ma1_1 = mst_st[7], ma2_1 = mst_st[8], mg_1 = mst_st[9];
        double mw1_0 = 0.0, mw2_0 = 0.0, mw1_1 = 0.0, mw2_1 = 0.0;

        for (int t = 0; t < n_frames; t++) {
            float sum = 0.0f;
            if (t + 320 < n_frames) {
                union { float f; int32_t i; } u; u.f = 1.0f + b0[t + 320] * sc0;
                sum += (float)(u.i - 0x3f800000) * cl0;
            }
            if (t + 64 < n_frames) {
                union { float f; int32_t i; } u; u.f = 1.0f + b1[t + 64] * sc1;
                sum += (float)(u.i - 0x3f800000) * cl1;
            }
            if (t + 32 < n_frames) {
                union { float f; int32_t i; } u; u.f = 1.0f + b2[t + 32] * sc2;
                sum += (float)(u.i - 0x3f800000) * cl2;
            }
            if (t < n_frames) {
                union { float f; int32_t i; } u; u.f = 1.0f + b3[t] * sc3;
                sum += (float)(u.i - 0x3f800000) * cl3;
            }
            double w0 = mg_0 * (double)sum - ma1_0 * mw1_0 - ma2_0 * mw2_0;
            double out0 = w0 + mb1_0 * mw1_0 + mb2_0 * mw2_0;
            mw2_0 = mw1_0; mw1_0 = w0;
            float x = (float)out0;
            w0 = mg_1 * (double)x - ma1_1 * mw1_1 - ma2_1 * mw2_1;
            double out1 = w0 + mb1_1 * mw1_1 + mb2_1 * mw2_1;
            mw2_1 = mw1_1; mw1_1 = w0;
            master_env[t] = (float)out1;
        }
        for (int b = 0; b < 4; b++) free(band_bufs[b]);
    } else {
        for (int b = 0; b < 4; b++) {
            accumulate_band_log_novelty(band_bufs[b], n_frames, delays[b], weights[b], medians[b], master_env, 0);
            free(band_bufs[b]);
        }
        cascade_biquad_inplace(master_env, n_frames, mst_st, 2);
    }

    resample_to_1000hz(master_env, n_frames, sample_rate, env_1000hz, n_dst);
    free(master_env);

    apply_zero_phase_diff(env_1000hz, n_dst, diff_1000hz);
}



