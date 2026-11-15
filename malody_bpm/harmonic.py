import math

# 逆向提取自 Malody 0x100432f10 的 7 级阶梯谐波消歧与浮点量化矩阵
# 每项为 (目标倍率 Ratio, 容差系数 Threshold)
HARMONIC_TIERS = [
    (1.0,   3.0),    # 整数 BPM (如 165.00, 166.00, 120.00, 148.00)
    (2.0,   2.5),    # 半整数 (如 122.50)
    (3.0,   2.0),    # 三分之一 (如 133.333...)
    (10.0,  2.0),    # 十分位 (如 175.10)
    (20.0,  1.5),    # 二十分位 (如 160.05)
    (100.0, 1.5),    # 百分位 (如 142.08, 160.99, 199.61, 201.76, 211.86)
    (200.0, 1.0),    # 二百分位 (如 134.925, 79.995)
]

def resolve_harmonic_quantization(
    raw_bpm: float,
    tolerance: float = 0.01,
    sigma_t: float | None = None,
    rel_instability: float | None = None
) -> tuple[float, bool]:
    """
    执行 Malody 0x1000c07ce 的 IEEE remainder 阶梯量化消歧算法。
    按精度从粗到细（整数 -> 0.5 -> 0.333 -> 0.1 -> 0.05 -> 0.01 -> 0.005）依次匹配：
    若门控检查通过且 raw_bpm 距某阶梯基准网格的偏差在 threshold * tolerance 门限内，
    则吸附至该精确分数/小数位并返回 (quantized_bpm, True)。
    若未命中任何阶梯或未通过门控，则直接返回 (raw_bpm, False)。
    """
    # 门控检查 (对齐 Malody 0x1000c05c9 & 0x1000c05d7)
    if sigma_t is not None and sigma_t > 2.4:
        return raw_bpm, False
    if rel_instability is not None and rel_instability > 5e-5:
        return raw_bpm, False

    for ratio, threshold in HARMONIC_TIERS:
        step = 1.0 / ratio
        # IEEE 754 remainder (对齐 C 标准库 remainder)
        rem = math.remainder(raw_bpm, step)
        abs_rem = abs(rem)
        
        if abs_rem < threshold * tolerance:
            # 锁定吸附至该比例的最近点 (对齐 C lrint)
            snapped = round(raw_bpm * ratio) / ratio
            return snapped, True

    return raw_bpm, False
