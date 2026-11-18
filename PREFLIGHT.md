# 提交前自检（PREFLIGHT）

> 自动生成于 `src/preflight.py`，共 16 项检查。
> 阻断项 2/13 通过；建议项 2/3 通过。

## 结论：还不能提交 ❌

## 一、硬性必交项

| 检查项 | 状态 | 实测 / 现状 |
|---|---|---|
| 仓库命名规范 | ✅ | 当前目录名 `ICEC-2026-0247-lihuanyi` |
| 原始录音 5 工况 × 15 组 | ❌ | N1:0  N2:0  F1:0  O1:0  S0:0　合计 0 个 |
| 三次会话 S1/S2/S3 各自独立 | ❌ | S1:0  S2:0  S3:0 |
| 文件命名全部合规 | ✅ | 不合规 0 个 |
| 实验现场照片 ≥6 张（含 EXIF） | ❌ | 共 0 张，其中 0 张带拍摄时间 |
| 跨日运行日志 run_log.jsonl | ❌ | 文件不存在 |
| 结果目录产物齐全 | ❌ | 缺 8 项：REPORT.md、metrics.json、data_qa.md、confusion_matrix.png、noise_robustness.png、waveform_spectra.png、feature_radar.png、file_level_predictions.csv |
| 盲测集 ≥10 个且命名合规 | ❌ | 0/0 个合规 |
| 课题02 原始记录表 ≥24 行 | ❌ | 0 行 |
| 课题10 耗时记录 ≥10 个样本 | ❌ | 0 行 |
| 第一阶段图表 | ❌ | 缺：T02-总览.png、T10-耗时对比.png |
| 第一阶段现场同框照 | ❌ | 0 张 |
| 文档里的 ▢ 占位符已填完 | ❌ | 还剩 52 处：02-20Days-Challenge\iterations\README.md(24)、README.md(21)、03-AI-Collaboration\README.md(4)、01-Field-Discovery\T10-数字任务耗时审计\README.md(2)、01-Field-Discovery\T02-手机多工况发热掉电\README.md(1) |

## 二、加分项

| 检查项 | 状态 | 实测 / 现状 |
|---|---|---|
| 单段时长 ≥5 秒 | ✅ | 偏短 0 个 |
| 盲测结果已落表 | ❌ | results/blind_test.csv |
| 仓库体积可控（<200MB，单文件<40MB） | ✅ | 0.3 MB |

## 三、现在该做什么（按顺序）

1. **原始录音 5 工况 × 15 组** —— 按 docs/01-录音与实验SOP.md 第 3 节补齐；每工况每个会话 5 段，共 5×3×5=75 个
2. **三次会话 S1/S2/S3 各自独立** —— LOSO 交叉验证需要三次独立实验；若只有一个房间，就至少换时段并在报告里如实写明
3. **实验现场照片 ≥6 张（含 EXIF）** —— 用手机原生相机拍，用数据线拷进来；经微信/QQ 传输会剥掉 EXIF
4. **跨日运行日志 run_log.jsonl** —— 跑一次 `python src/run_all.py`，日志由 logbook.py 自动追加；连续 3 天各跑一次
5. **结果目录产物齐全** —— 跑 `python src/run_all.py`，全部图表与报告由脚本生成，不要手改
6. **盲测集 ≥10 个且命名合规** —— 盲测集不能参与训练；命名 BLIND_<真实工况>_<序号>.wav，5 工况各 2~3 个
7. **课题02 原始记录表 ≥24 行** —— 用 collect_t02_adb.py 自动采（adb 读电池，时间戳由电脑打），或手工按示例格式填
8. **课题10 耗时记录 ≥10 个样本** —— 复制 raw_data/耗时记录-示例格式.csv → 耗时记录.csv，清空示例数字后填实测值
9. **第一阶段图表** —— `python analyze_t02.py --mah 4500` / `python analyze_t10.py`
10. **第一阶段现场同框照** —— 两题各至少 1 张：拍到手 + 设备屏幕/场景，放 01-Field-Discovery/figures/
11. **文档里的 ▢ 占位符已填完** —— 这些必须是你自己的数字与判断（准确率、局限、策略收益），不能由脚本代填
12. **盲测结果已落表** —— 跑 `python src/predict_live.py --dir raw_blind`，并录一段不剪辑的视频

## 四、提交命令

```bash
python src/preflight.py          # 再跑一次，确认没有 ❌
git add -A
git commit -m "docs: 补齐原始数据与报告，自检全绿"
git push
```
