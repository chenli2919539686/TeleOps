import json

def run(params: dict) -> dict:
    peer_ip = params.get('peer_ip', '')
    interface = params.get('interface', '')
    if not peer_ip or not interface:
        return {'error': 'peer_ip and interface are required'}
    # 模拟探测结果（实际应通过SNMP/CLI获取）
    rx_power = -18.5  # 接收光功率 dBm
    tx_power = 1.2    # 发送光功率 dBm
    rx_threshold = -25.0  # 接收告警阈值
    tx_threshold = -5.0   # 发送告警阈值
    status = 'normal'
    if rx_power < rx_threshold or tx_power < tx_threshold:
        status = 'degraded'
    return {
        'peer_ip': peer_ip,
        'interface': interface,
        'rx_power_dbm': rx_power,
        'tx_power_dbm': tx_power,
        'rx_threshold_dbm': rx_threshold,
        'tx_threshold_dbm': tx_threshold,
        'status': status,
        'suggestion': '检查光路和连接器' if status == 'degraded' else '光路正常'
    }
