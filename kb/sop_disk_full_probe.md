# 磁盘空间告警故障处置SOP

## 1. 告警信息

- **告警名称**: A-DISK-FULL-001
- **影响业务**: db-order（物理机）
- **严重级别**: P1（紧急）
- **告警触发条件**: 磁盘使用率超过阈值（默认85%）

## 2. 故障确认

### 2.1 初步检查
```bash
# 查看磁盘整体使用情况
df -h

# 确认挂载点使用率
df -hT | awk '$6 > 85 {print}'
```

### 2.2 告警确认标准
- 任一挂载点使用率 ≥ 85%
- 或磁盘剩余空间 < 10GB（视业务需求调整）

## 3. 根因分析（按优先级排查）

### 3.1 日志文件膨胀（置信度：75%）
```bash
# 查看 /var/log 下各目录大小
du -sh /var/log/*

# 查看具体大文件
du -sh /var/log/*/* | sort -rh | head -20

# 检查 logrotate 配置
cat /etc/logrotate.conf
ls -la /etc/logrotate.d/
```

**处理措施**：
- 确认大日志文件（>1GB）
- 检查 logrotate 是否正常执行
- 手动触发 logrotate：`logrotate -f /etc/logrotate.conf`
- 必要时清理历史日志：`find /var/log -name "*.gz" -mtime +7 -delete`

### 3.2 临时文件堆积（置信度：15%）
```bash
# 检查 /tmp 目录
du -sh /tmp/* | sort -rh | head -20

# 检查其他临时目录
du -sh /var/tmp/* | sort -rh | head -20

# 清理过期临时文件
find /tmp -type f -mtime +3 -delete
```

### 3.3 异常进程写入（置信度：10%）
```bash
# 查找已删除但仍被占用的文件
lsof | grep deleted

# 查看占用空间最大的进程
lsof +L1 | sort -k7 -rn | head -20
```

**处理措施**：
- 确认占用进程
- 重启相关服务释放文件句柄
- 若为异常进程，需kill并排查原因

## 4. 应急处理流程

### 4.1 快速释放空间（5分钟内）
```bash
# 1. 清理系统临时文件
rm -rf /tmp/* 2>/dev/null
rm -rf /var/tmp/* 2>/dev/null

# 2. 清理yum缓存
yum clean all 2>/dev/null

# 3. 清理journal日志（保留最近2天）
journalctl --vacuum-time=2d

# 4. 清理core dump文件
find / -name "core.*" -type f -mtime +1 -delete 2>/dev/null
```

### 4.2 日志文件专项处理
```bash
# 1. 定位大文件
du -sh /var/log/* | sort -rh | head -10

# 2. 截断大日志文件（保留最新内容）
LOG_FILE="/var/log/xxx.log"
> $LOG_FILE

# 3. 压缩归档
gzip $LOG_FILE.$(date +%Y%m%d)
```

### 4.3 空间确认
```bash
# 确认空间已释放
df -h
```

## 5. 根因定位与修复

### 5.1 日志膨胀根因
```bash
# 检查应用日志配置
grep -r "log" /etc/rsyslog.conf
grep -r "log" /etc/logrotate.d/

# 检查应用日志级别
# 确认是否有错误循环写入
tail -f /var/log/xxx.log | head -100
```

**修复措施**：
- 调整日志级别（如从DEBUG改为INFO）
- 配置合理的logrotate策略
- 增加日志轮转频率

### 5.2 进程异常根因
```bash
# 查看进程详情
ps aux | grep <PID>
ls -la /proc/<PID>/fd/

# 检查应用配置
# 确认是否有文件句柄泄漏
```

**修复措施**：
- 修复应用bug
- 调整文件描述符限制
- 添加监控告警

## 6. 预防措施

### 6.1 监控优化
- 设置磁盘使用率分级告警（70%警告、80%严重、90%紧急）
- 添加日志文件大小监控
- 监控logrotate执行状态

### 6.2 自动化清理
```bash
# 添加crontab定时任务
# 每日清理过期日志
0 2 * * * find /var/log -name "*.log.*" -mtime +7 -delete
# 每周清理临时文件
0 3 * * 0 find /tmp -type f -mtime +7 -delete
```

### 6.3 配置优化
- 统一日志管理方案（如ELK）
- 配置合理的logrotate策略
- 定期审查应用日志配置

## 7. 故障报告模板

```markdown
## 故障报告

### 基本信息
- 告警时间：
- 恢复时间：
- 影响时长：
- 影响范围：

### 根因分析
- 直接原因：
- 根本原因：
- 触发条件：

### 处理过程
1. 
2. 
3. 

### 改进措施
1. 
2. 
3. 
```

## 8. 注意事项

1. **禁止直接删除正在使用的日志文件**，应先截断或轮转
2. **清理前确认文件用途**，避免误删重要数据
3. **操作前备份关键配置**，便于回滚
4. **记录所有操作步骤**，便于复盘
5. **处理后持续监控**，确认问题不再复发

## 9. 相关工具

| 工具 | 用途 | 优先级 |
|------|------|--------|
| df | 查看磁盘使用率 | 必用 |
| du | 查看目录大小 | 必用 |
| lsof | 查看文件占用 | 必用 |
| logrotate | 日志轮转 | 按需 |
| journalctl | 系统日志管理 | 按需 |

## 10. 升级条件

- 磁盘使用率达到95%以上
- 空间释放操作后仍无法恢复
- 影响核心业务（如db-order）
- 需要重启关键服务

满足以上任一条件，需立即升级至二线运维团队处理。