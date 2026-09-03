import subprocess
import os
import json
import shlex
from datetime import datetime

def run(params: dict) -> dict:
    """
    磁盘满探测工具：
    1. df -h 查看整体磁盘使用情况
    2. du -sh <log_dir>/* 检查日志目录下各文件/子目录大小
    3. lsof | grep deleted 查找已删除但仍被进程占用的文件
    """
    log_dir = params.get("log_dir", "/var/log")
    top_n = int(params.get("top_n", 10))
    timeout = int(params.get("timeout", 30))

    result = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "tool": "disk_full_probe",
        "status": "success",
        "data": {},
        "errors": []
    }

    # 1. df -h
    try:
        df_cmd = ["df", "-h"]
        df_output = subprocess.run(df_cmd, capture_output=True, text=True, timeout=timeout)
        if df_output.returncode == 0:
            result["data"]["df_h"] = df_output.stdout.strip()
        else:
            result["errors"].append(f"df -h failed: {df_output.stderr.strip()}")
    except Exception as e:
        result["errors"].append(f"df -h error: {str(e)}")

    # 2. du -sh <log_dir>/*
    try:
        # 检查目录是否存在
        if not os.path.isdir(log_dir):
            result["errors"].append(f"Directory {log_dir} does not exist")
        else:
            # 使用 du 命令，避免 shell 通配符问题
            du_cmd = ["du", "-sh", log_dir + "/*"]
            du_output = subprocess.run(du_cmd, capture_output=True, text=True, timeout=timeout, shell=False)
            if du_output.returncode == 0:
                lines = du_output.stdout.strip().split('\n')
                # 解析大小和路径
                parsed = []
                for line in lines:
                    if not line.strip():
                        continue
                    parts = line.split('\t')
                    if len(parts) >= 2:
                        size = parts[0]
                        path = parts[1]
                        # 尝试转换为字节数用于排序
                        try:
                            size_num = float(size[:-1]) * {
                                'K': 1024, 'M': 1024**2, 'G': 1024**3, 'T': 1024**4,
                                'k': 1024, 'm': 1024**2, 'g': 1024**3, 't': 1024**4
                            }.get(size[-1], 1)
                        except:
                            size_num = 0
                        parsed.append({"size": size, "path": path, "bytes": size_num})
                
                # 按大小排序取 top N
                parsed.sort(key=lambda x: x["bytes"], reverse=True)
                result["data"]["du_log_dir"] = parsed[:top_n]
            else:
                result["errors"].append(f"du -sh {log_dir}/* failed: {du_output.stderr.strip()}")
    except Exception as e:
        result["errors"].append(f"du error: {str(e)}")

    # 3. lsof | grep deleted
    try:
        # 使用 lsof 命令，过滤 deleted 文件
        lsof_cmd = ["lsof", "+L1"]  # +L1 列出 link count < 1 的文件（即已删除但仍打开）
        lsof_output = subprocess.run(lsof_cmd, capture_output=True, text=True, timeout=timeout)
        if lsof_output.returncode == 0:
            lines = lsof_output.stdout.strip().split('\n')
            deleted_files = []
            for line in lines[1:]:  # 跳过标题行
                if not line.strip():
                    continue
                # 解析关键字段：COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME
                parts = line.split()
                if len(parts) >= 9:
                    deleted_files.append({
                        "command": parts[0],
                        "pid": parts[1],
                        "user": parts[2],
                        "fd": parts[3],
                        "type": parts[4],
                        "device": parts[5],
                        "size_off": parts[6],
                        "node": parts[7],
                        "name": parts[8]
                    })
            result["data"]["deleted_open_files"] = deleted_files[:top_n]
        else:
            # lsof 可能返回非零码（如无匹配），但 stderr 可能为空
            if lsof_output.stderr.strip():
                result["errors"].append(f"lsof error: {lsof_output.stderr.strip()}")
            else:
                result["data"]["deleted_open_files"] = []
    except FileNotFoundError:
        result["errors"].append("lsof command not found")
    except Exception as e:
        result["errors"].append(f"lsof error: {str(e)}")

    # 汇总状态
    if result["errors"]:
        result["status"] = "partial_success" if result["data"] else "failed"

    return result
