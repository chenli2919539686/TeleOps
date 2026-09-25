"""下载并解压 uccmisl/5Gdataset 到 data/5g_dataset/（真实电信数据接入的取数脚本）。

用法：
  python scripts/fetch_5g_dataset.py            # 下载 + 解压到 data/5g_dataset
  python scripts/fetch_5g_dataset.py --check    # 仅打印数据集元信息（不下载）

数据源：https://github.com/uccmisl/5Gdataset  （GPL-3.0，仅内用，不入库）
解压后数据集目录可直接配进 data/adapters.json[alert-5g].dataset_path。

注意：数据集文件较大且含原始公开日志，已加入 .gitignore（data/5g_dataset/），
不会被提交；需要时用本脚本重新拉取即可。
"""
import argparse
import io
import os
import sys
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEST = os.path.join(ROOT, "data", "5g_dataset")

# 主源 + 兜底（Zenodo 是整个仓库 zip，内含 5G-production-dataset.zip）
URLS = [
    "https://github.com/uccmisl/5Gdataset/raw/master/5G-production-dataset.zip",
    "https://zenodo.org/records/3751187/files/uccmisl%2F5Gdataset-v1.0.0.zip",
]
TOP_FOLDER = "5G-production-dataset/"


def _iter_csv_names(zf: zipfile.ZipFile):
    for n in zf.namelist():
        base = n[len(TOP_FOLDER):] if n.startswith(TOP_FOLDER) else n
        if base.startswith("__MACOSX") or n.endswith(".DS_Store") or not base:
            continue
        if base.lower().endswith(".csv"):
            yield n, base


def summarize(zf: zipfile.ZipFile):
    csvs = list(_iter_csv_names(zf))
    print(f"[fetch] 数据集内 CSV 文件数：{len(csvs)}")
    if csvs:
        with zf.open(csvs[0][0]) as fh:
            head = fh.read(400).decode("utf-8", "replace").replace("\r", "")
        print(f"[fetch] 示例表头（{csvs[0][1]}）：\n  {head.splitlines()[0] if head else ''}")
    print("[fetch] 指标列（G-NetTrack Pro）：RSRP/RSRQ/SNR/CQI/RSSI/DL_bitrate/UL_bitrate/PINGLOSS/CellID ...")
    print("[fetch] License：GPL-3.0 —— 仅内部使用，不随仓库分发；data/5g_dataset/ 已在 .gitignore。")


def fetch():
    os.makedirs(DEST, exist_ok=True)
    zippath = os.path.join(DEST, "5G-production-dataset.zip")
    if os.path.exists(zippath) and not os.environ.get("FORCE_DOWNLOAD"):
        print(f"[fetch] 已存在 {zippath}，跳过下载（设 FORCE_DOWNLOAD=1 强制重下）")
    else:
        last_err = None
        for url in URLS:
            try:
                print(f"[fetch] 下载 {url}")
                data = urllib.request.urlopen(url, timeout=120).read()
                open(zippath, "wb").write(data)
                print(f"[fetch] 已保存 {len(data)} 字节 -> {zippath}")
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                print(f"[fetch] 失败：{e}")
        else:
            print(f"[fetch] 全部源下载失败：{last_err}", file=sys.stderr)
            return 1
    with zipfile.ZipFile(zippath) as zf:
        summarize(zf)
        print(f"[fetch] 解压到 {DEST} ...")
        extracted = 0
        for name, base in _iter_csv_names(zf):
            target = os.path.join(DEST, base)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(name) as src, open(target, "wb") as out:
                out.write(src.read())
            extracted += 1
        print(f"[fetch] 解压完成，CSV 数：{extracted}")
    print(f"[fetch] 下一步：在 data/adapters.json 增加 \"alert-5g\": {{\"dataset_path\": \"data/5g_dataset\"}}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="取数 uccmisl/5Gdataset")
    ap.add_argument("--check", action="store_true", help="仅打印元信息")
    args = ap.parse_args()
    if args.check:
        # 若本地已有 zip 就直接 summarize，否则提示先下载
        zippath = os.path.join(DEST, "5G-production-dataset.zip")
        if os.path.exists(zippath):
            with zipfile.ZipFile(zippath) as zf:
                summarize(zf)
            return 0
        print("[fetch] 本地无数据集，先运行 python scripts/fetch_5g_dataset.py 下载")
        return 0
    return fetch()


if __name__ == "__main__":
    sys.exit(main())
