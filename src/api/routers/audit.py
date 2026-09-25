"""操作审计日志端点（多租户问责）：谁在何时对哪个业务域做了什么。

从 server.py 抽出（D2 演进式拆分 R1）。ws_store 经 `s.` 引用，
确保走最新的业务域可见性判定（含组织树 + RBAC）。

提供两类视图：
- GET /audit          倒序列表（运维/合规日常查阅，最新在前）
- GET /audit/timeline 正序时间线（"回放"用：像看录像带一样按时间顺序重现操作，
                       并附密度分桶 + 摘要统计，便于快速定位异常时段）
"""
import csv
import datetime
import io
import json
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from fastapi import Body

from src.core import db, oss
from src.api.context import ctx as s

router = APIRouter()

# 审计 CSV 列顺序（detail 为 JSON 字符串原样落库，导出时按字符串写入）
CSV_COLUMNS = ["id", "ts", "actor", "actor_id", "action",
               "workspace_id", "detail", "result", "ip"]


def _base_where(user: dict):
    """返回 (where, params) 业务域隔离条件（不含 since/until/actor/action 等过滤）。

    隔离规则（与读写隔离同口径）：
    - 管理员：空条件（看全量）
    - 普通用户：只看「自己可见业务域」的操作 + 自己的认证类记录
      （auth.login / auth.logout / auth.register 等 workspace_id 为空的动作）
    - 匿名：由调用方先行 401 拦截
    """
    is_admin = bool(user.get("is_admin"))
    if is_admin:
        return "", []
    visible = s.ws_store.visible_workspace_ids(user=user)
    if not visible:
        return "WHERE 1=0", []
    ph = ",".join("?" * len(visible))
    where = (f"WHERE (workspace_id IN ({ph}) "
             "OR (workspace_id IS NULL AND actor_id=?))")
    return where, list(visible) + [user.get("uid")]


def _apply_filters(where: str, params: list, *, actor=None,
                   action_prefix=None, since=None, until=None):
    """在隔离条件之上叠加可选过滤（actor / action_prefix / since / until）。"""
    extra = []
    if actor:
        extra.append("actor=?")
        params = list(params) + [actor]
    if action_prefix:
        extra.append("action LIKE ?")
        params = list(params) + [action_prefix + "%"]
    if since:
        extra.append("ts >= ?")
        params = list(params) + [since]
    if until:
        extra.append("ts <= ?")
        params = list(params) + [until]
    if extra:
        w2 = " AND ".join(extra)
        where = f"{where} AND {w2}" if where else f"WHERE {w2}"
    return where, params


def _norm_row(r) -> dict:
    """把审计行规范化：detail 若为 JSON 字符串则解析为对象，便于前端结构化展示。"""
    d = dict(r)
    raw = d.get("detail")
    if isinstance(raw, str):
        try:
            d["detail"] = json.loads(raw)
        except (ValueError, TypeError):
            pass
    return d


def _bucket(items: list, unit: str):
    """按 minute/hour/day 把事件聚合成密度分桶（在 Python 侧聚合，规避 SQLite/PG 方言差异）。"""
    if unit not in ("minute", "hour", "day"):
        return []
    buckets = {}
    for it in items:
        ts = it.get("ts")
        if not ts:
            continue
        try:
            dt = datetime.datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            continue
        if unit == "minute":
            key = dt.replace(second=0, microsecond=0)
        elif unit == "hour":
            key = dt.replace(minute=0, second=0, microsecond=0)
        else:
            key = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        b = buckets.setdefault(
            key, {"count": 0, "by_result": {"ok": 0, "denied": 0, "error": 0}})
        b["count"] += 1
        res = it.get("result", "ok") or "ok"
        b["by_result"][res] = b["by_result"].get(res, 0) + 1
    return [{"t": k.isoformat(), **v} for k, v in sorted(buckets.items())]


def _summarize(items: list) -> dict:
    by_result = {"ok": 0, "denied": 0, "error": 0}
    actions, actors = {}, {}
    for it in items:
        res = it.get("result", "ok") or "ok"
        by_result[res] = by_result.get(res, 0) + 1
        a = it.get("action", "?")
        actions[a] = actions.get(a, 0) + 1
        act = it.get("actor", "?")
        actors[act] = actors.get(act, 0) + 1
    return {
        "total": len(items),
        "by_result": by_result,
        "top_actions": sorted(actions.items(), key=lambda x: -x[1])[:10],
        "top_actors": sorted(actors.items(), key=lambda x: -x[1])[:10],
    }


@router.get("/audit")
def list_audit(request: Request, limit: int = 50, offset: int = 0,
               workspace_id: Optional[str] = None,
               since: Optional[str] = None, until: Optional[str] = None,
               actor: Optional[str] = None,
               action_prefix: Optional[str] = None):
    """操作审计日志（多租户问责）：倒序列表，最新在前。

    隔离规则（与读写隔离同口径）：
    - 管理员：默认看全量，可按 workspace_id / actor / action_prefix 过滤
    - 普通用户：只看「自己可见业务域」的操作 + 自己的认证类记录
      （auth.login / auth.logout / auth.register 等 workspace_id 为空的动作）
    - 匿名：401
    - 指定 workspace_id 时先校验可见性，越权/不存在 → 404（不暴露域是否存在）

    since/until：ISO 字符串，按 ts 字典序过滤（兼容 ISO 格式）。
    actor：精确匹配操作人；action_prefix：动作前缀匹配（如 "auth." / "workspace."）。
    """
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="需要登录才能查看审计日志")

    if workspace_id:
        if not s.ws_store.is_visible_to(workspace_id, user):
            raise HTTPException(status_code=404, detail="业务域不存在")
        where, params = "WHERE workspace_id=?", [workspace_id]
    else:
        where, params = _base_where(user)

    where, params = _apply_filters(
        where, params, actor=actor, action_prefix=action_prefix,
        since=since, until=until)

    safe_limit = max(1, min(int(limit), 500))
    safe_offset = max(0, int(offset))
    rows = db.query(
        f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        tuple(params) + (safe_limit, safe_offset))
    total = db.query_one(f"SELECT COUNT(*) AS c FROM audit_log {where}",
                         tuple(params))["c"]
    return {"items": [dict(r) for r in rows], "total": total,
            "limit": safe_limit, "offset": safe_offset,
            "scope": "all" if user.get("is_admin") else "own"}


@router.get("/audit/timeline")
def audit_timeline(request: Request, limit: int = 500, offset: int = 0,
                   workspace_id: Optional[str] = None,
                   actor: Optional[str] = None,
                   action_prefix: Optional[str] = None,
                   since: Optional[str] = None, until: Optional[str] = None,
                   bucket: Optional[str] = None):
    """可回放审计时间线（对标「像看录像带一样重现操作」）。

    与 /audit 的差别：
    - 返回**正序**（id ASC，即操作发生的真实时序），供回放逐条重现；
    - 附 `buckets` 密度分桶（bucket=minute/hour/day，Python 侧聚合，方言无关），
      用于在前端画时间轴热力条、快速定位异常密集时段；
    - 附 `summary` 摘要（总数 / 结果分布 / Top 动作 / Top 操作人）。

    隔离规则、过滤参数（workspace_id/actor/action_prefix/since/until）与 /audit 完全一致。
    detail 若为 JSON 字符串会解析为对象返回。
    """
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="需要登录才能查看审计日志")

    if workspace_id:
        if not s.ws_store.is_visible_to(workspace_id, user):
            raise HTTPException(status_code=404, detail="业务域不存在")
        where, params = "WHERE workspace_id=?", [workspace_id]
    else:
        where, params = _base_where(user)

    where, params = _apply_filters(
        where, params, actor=actor, action_prefix=action_prefix,
        since=since, until=until)

    safe_limit = max(1, min(int(limit), 2000))
    safe_offset = max(0, int(offset))
    rows = db.query(
        f"SELECT * FROM audit_log {where} ORDER BY id ASC LIMIT ? OFFSET ?",
        tuple(params) + (safe_limit, safe_offset))
    total = db.query_one(f"SELECT COUNT(*) AS c FROM audit_log {where}",
                         tuple(params))["c"]
    items = [_norm_row(r) for r in rows]
    return {
        "items": items,
        "total": total,
        "buckets": _bucket(items, bucket) if bucket else [],
        "summary": _summarize(items),
        "scope": "all" if user.get("is_admin") else "own",
        "range": {"since": since, "until": until},
        "bucket": bucket,
    }


def _csv_cell(value) -> str:
    """CSV 单元格标准化：None→空串，其余转字符串（detail 已是 JSON 串原样导出）。"""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _iter_csv(rows):
    """流式生成 CSV：首块带 UTF-8 BOM（Excel 直接打开不乱码），随后逐行写出。"""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    yield "\ufeff" + buf.getvalue()
    for r in rows:
        d = dict(r)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([_csv_cell(d.get(c)) for c in CSV_COLUMNS])
        yield buf.getvalue()


def _build_csv_bytes(rows) -> bytes:
    """把审计行打包成 UTF-8（含 BOM）CSV 字节——供「直接下载」与「归档 OSS」共用。"""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    for r in rows:
        d = dict(r)
        w.writerow([_csv_cell(d.get(c)) for c in CSV_COLUMNS])
    return ("\ufeff" + buf.getvalue()).encode("utf-8-sig")


def _collect_rows(user: dict, *, workspace_id=None, actor=None,
                  action_prefix=None, since=None, until=None):
    """按隔离 + 过滤取「全部匹配」审计行（导出/归档共用，保证看得到的才能导出）。"""
    if workspace_id:
        if not s.ws_store.is_visible_to(workspace_id, user):
            return None  # 越权/不存在，调用方转 404
        where, params = "WHERE workspace_id=?", [workspace_id]
    else:
        where, params = _base_where(user)
    where, params = _apply_filters(
        where, params, actor=actor, action_prefix=action_prefix,
        since=since, until=until)
    return db.query(
        f"SELECT * FROM audit_log {where} ORDER BY id ASC", tuple(params))


@router.get("/audit/export")
def export_audit(request: Request,
                 format: str = "csv",
                 workspace_id: Optional[str] = None,
                 actor: Optional[str] = None,
                 action_prefix: Optional[str] = None,
                 since: Optional[str] = None, until: Optional[str] = None):
    """导出审计日志（多租户问责）：按当前筛选条件导出「全部匹配记录」，不做分页截断。

    与 /audit、/audit/timeline 共用同一套隔离与过滤逻辑（_base_where + _apply_filters），
    保证「看得到的才能导出」，绝不越权泄露他人业务域记录。

    - format=csv（默认）：UTF-8（含 BOM）CSV，浏览器触发文件下载；
      列：id, ts, actor, actor_id, action, workspace_id, detail, result, ip。
    - format=json：返回相同记录数组（便于程序化消费）。
    - 隔离规则、过滤参数（workspace_id/actor/action_prefix/since/until）与 /audit 完全一致。
    - 零外部依赖、零配置，开箱即用（契合项目「配置驱动 + 零依赖可演示」哲学）。

    说明：审计导出不需要外部对象存储——直接经 HTTP 流式落盘到本机，是纯本地能力；
    若后续要做「导出到 OSS」，那是另一条独立线（同一套筛选 SQL 复用即可）。
    """
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="需要登录才能导出审计日志")

    rows = _collect_rows(
        user, workspace_id=workspace_id, actor=actor,
        action_prefix=action_prefix, since=since, until=until)
    if rows is None:
        raise HTTPException(status_code=404, detail="业务域不存在")

    if format == "json":
        return Response(
            content=json.dumps([dict(r) for r in rows], ensure_ascii=False, default=str),
            media_type="application/json")

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    headers = {
        "Content-Disposition": f'attachment; filename="teleops-audit-{stamp}.csv"',
    }
    return StreamingResponse(
        _iter_csv(rows), media_type="text/csv; charset=utf-8", headers=headers)


@router.post("/audit/archive")
def archive_audit(request: Request, payload: dict = Body(default={})):
    """归档审计日志到对象存储（OSS/S3）：配置驱动 + 本地 mock 兜底。

    复用 /audit、/audit/export 同一套隔离与过滤（_base_where + _apply_filters +
    _collect_rows），保证「看得到的才能归档」，绝不越权泄露他人业务域记录。

    请求体（均可选，缺省按当前登录用户可见范围全量）：
      { format: "csv"|"json"(默认 csv),
        workspace_id, actor, action_prefix, since, until,
        key?: 自定义对象 key（缺省用 oss.default_audit_key） }

    行为（取决于 oss_mode）：
      - off ：未启用 OSS → 503，提示设置 TELEOPS_OSS_ENABLED=1；
      - mock：写本地 data/oss_mock/<key>，零依赖可演示（url 为本地路径）；
      - s3  ：boto3 上传到真实桶，url 为预签名 GET URL（1h 有效）。

    返回 { backend, mode, key, bucket?, local_path?, url, bytes, count }。
    """
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="需要登录才能归档审计日志")

    fmt = str(payload.get("format", "csv")).lower()
    if fmt not in ("csv", "json"):
        fmt = "csv"
    rows = _collect_rows(
        user,
        workspace_id=payload.get("workspace_id"),
        actor=payload.get("actor"),
        action_prefix=payload.get("action_prefix"),
        since=payload.get("since"),
        until=payload.get("until"))
    if rows is None:
        raise HTTPException(status_code=404, detail="业务域不存在")

    if fmt == "json":
        data = json.dumps([dict(r) for r in rows], ensure_ascii=False,
                          default=str).encode("utf-8")
        content_type = "application/json"
        ext = "json"
    else:
        data = _build_csv_bytes(rows)
        content_type = "text/csv; charset=utf-8"
        ext = "csv"

    key = payload.get("key") or oss.default_audit_key(ext)
    try:
        info = oss.archive_bytes(key, data, content_type)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    info["bytes"] = len(data)
    info["count"] = len(rows)
    return info
