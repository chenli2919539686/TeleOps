"""对象存储（OSS/S3）归档后端：配置驱动 + 本地 mock 兜底，零外部依赖可演示。

设计哲学与 OIDC 完全一致——「配置驱动 + mock 兜底」：
- 不配任何东西（默认）：oss_mode() == "off"，归档接口返回 503 并提示如何启用；
- 仅设 TELEOPS_OSS_ENABLED=1（无真实桶）：oss_mode() == "mock"，
  把归档对象写到本地 data/oss_mock/ 目录，**零外部依赖、零凭据即可演示**；
- 设齐 TELEOPS_OSS_BUCKET / ENDPOINT / ACCESS_KEY / SECRET_KEY：oss_mode() == "s3"，
  走 S3 兼容协议（Aliyun OSS / MinIO / AWS S3 通用），懒加载 boto3，
  仅在真正用云时才 import boto3（未装则给出清晰安装提示，不影响 mock/关闭态）。

所有配置在调用时从 os.environ 读取（非模块级常量），便于单测 monkeypatch。
"""
import os
import datetime
from typing import Optional

# 运行时数据目录（与 db / teleops.db 同级）：mock 兜底落盘位置
_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "data")
_MOCK_ROOT = os.path.join(_DATA_DIR, "oss_mock")


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(name, "")
    return v.strip() if v else default


def oss_enabled() -> bool:
    """是否显式启用 OSS（ENABLED=1 即启用；或已配齐桶+端点也视为启用）。"""
    if _env("TELEOPS_OSS_ENABLED") == "1":
        return True
    return bool(_env("TELEOPS_OSS_BUCKET") and _env("TELEOPS_OSS_ENDPOINT"))


def oss_mode() -> str:
    """off | mock | s3 —— 三态判定。

    - off ：完全未配置（默认）。
    - s3  ：配齐 bucket + endpoint + access_key + secret_key，走真实云存储。
    - mock：启用但未配真实桶（仅 ENABLED=1），写本地目录兜底。
    """
    if not oss_enabled():
        return "off"
    if (_env("TELEOPS_OSS_BUCKET") and _env("TELEOPS_OSS_ENDPOINT")
            and _env("TELEOPS_OSS_ACCESS_KEY") and _env("TELEOPS_OSS_SECRET_KEY")):
        return "s3"
    return "mock"


def oss_config_summary() -> dict:
    """暴露给 /auth/status、/oss/status 的非敏感配置摘要（绝不回显密钥）。"""
    return {
        "oss_enabled": oss_enabled(),
        "oss_mode": oss_mode(),
        "bucket": _env("TELEOPS_OSS_BUCKET") or None,
        "endpoint": _env("TELEOPS_OSS_ENDPOINT") or None,
        "prefix": _env("TELEOPS_OSS_PREFIX") or "",
    }


def _safe_key(key: str) -> str:
    """规整对象 key：防目录穿越（../），统一用 / 分隔，去掉开头 / 。"""
    parts = [p for p in key.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return "/".join(parts)


def _mock_path(key: str) -> str:
    return os.path.join(_MOCK_ROOT, _safe_key(key))


def archive_bytes(key: str, data: bytes, content_type: str = "application/octet-stream") -> dict:
    """归档一段字节到 OSS/mock。

    返回 {backend, mode, key, bucket?, local_path?, url?}：
    - mock：写 data/oss_mock/<key>，url 为本地绝对路径（演示可直接打开）；
    - s3  ：boto3 put_object，url 为预签名 GET URL（默认 1 小时有效）；
    - off ：抛 RuntimeError，由调用方转 503。
    """
    mode = oss_mode()
    safe = _safe_key(key)
    if mode == "off":
        raise RuntimeError(
            "OSS 未启用：设置 TELEOPS_OSS_ENABLED=1 启用（mock 兜底），"
            "或配齐 TELEOPS_OSS_BUCKET/ENDPOINT/ACCESS_KEY/SECRET_KEY 走真实云存储")
    if mode == "mock":
        path = _mock_path(safe)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return {"backend": "mock", "mode": "mock", "key": safe,
                "local_path": path, "url": "file://" + path}
    # s3
    return _archive_s3(safe, data, content_type)


def _archive_s3(key: str, data: bytes, content_type: str) -> dict:
    try:
        import boto3  # 懒加载：仅在用真实云时才需要
    except ImportError:
        raise RuntimeError(
            "真实 OSS 需要 boto3：在隔离环境执行 "
            "`pip install boto3` 后重试（mock 模式无需此依赖）")
    bucket = _env("TELEOPS_OSS_BUCKET")
    endpoint = _env("TELEOPS_OSS_ENDPOINT")
    region = _env("TELEOPS_OSS_REGION") or "cn-hangzhou"
    prefix = _env("TELEOPS_OSS_PREFIX") or ""
    full_key = (prefix.rstrip("/") + "/" + key).lstrip("/") if prefix else key
    client = boto3.client(
        "s3",
        endpoint_url=(("https://" + endpoint) if not endpoint.startswith("http")
                      else endpoint),
        aws_access_key_id=_env("TELEOPS_OSS_ACCESS_KEY"),
        aws_secret_access_key=_env("TELEOPS_OSS_SECRET_KEY"),
        region_name=region,
    )
    client.put_object(Bucket=bucket, Key=full_key, Body=data,
                      ContentType=content_type)
    url = client.generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": full_key},
        ExpiresIn=3600)
    return {"backend": "s3", "mode": "s3", "key": full_key, "bucket": bucket,
            "url": url}


def object_url(key: str) -> Optional[str]:
    """给定 key 返回可访问 URL（mock 返回本地路径；s3 返回预签名 URL）。"""
    mode = oss_mode()
    safe = _safe_key(key)
    if mode == "mock":
        path = _mock_path(safe)
        return "file://" + path if os.path.exists(path) else None
    if mode == "s3":
        try:
            import boto3
        except ImportError:
            return None
        bucket = _env("TELEOPS_OSS_BUCKET")
        endpoint = _env("TELEOPS_OSS_ENDPOINT")
        region = _env("TELEOPS_OSS_REGION") or "cn-hangzhou"
        prefix = _env("TELEOPS_OSS_PREFIX") or ""
        full_key = (prefix.rstrip("/") + "/" + safe).lstrip("/") if prefix else safe
        client = boto3.client(
            "s3",
            endpoint_url=(("https://" + endpoint) if not endpoint.startswith("http")
                          else endpoint),
            aws_access_key_id=_env("TELEOPS_OSS_ACCESS_KEY"),
            aws_secret_access_key=_env("TELEOPS_OSS_SECRET_KEY"),
            region_name=region,
        )
        return client.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": full_key},
            ExpiresIn=3600)
    return None


def default_audit_key(ext: str = "csv") -> str:
    """生成审计归档对象的默认 key（按日期分目录，避免单目录膨胀）。"""
    d = datetime.datetime.now()
    stamp = d.strftime("%Y%m%d-%H%M%S")
    return f"audit/{d.strftime('%Y-%m-%d')}/teleops-audit-{stamp}.{ext}"
