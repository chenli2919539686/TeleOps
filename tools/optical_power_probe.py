def run(params: dict) -> dict:
    olt_id = params.get('olt_id', 'OLT-1')
    onu_id = params.get('onu_id', 'ONU-1')
    direction = params.get('direction', 'both')

    # 模拟探测结果（实际应调用设备接口获取真实光功率）
    # 下行：OLT发送 -> ONU接收；上行：ONU发送 -> OLT接收
    downlink_rx = -28.0  # ONU接收光功率(dBm)
    downlink_tx = 2.5    # OLT发送光功率(dBm)
    uplink_rx = -26.5    # OLT接收光功率(dBm)
    uplink_tx = 1.8      # ONU发送光功率(dBm)

    result = {
        'olt_id': olt_id,
        'onu_id': onu_id,
        'probe_time': '2025-01-01T00:00:00Z',
        'status': 'ok'
    }

    if direction in ('down', 'both'):
        result['downlink'] = {
            'tx_power_dbm': downlink_tx,
            'rx_power_dbm': downlink_rx,
            'link_loss_dbm': round(downlink_tx - downlink_rx, 2),
            'rx_status': 'degraded' if downlink_rx < -25 else 'normal'
        }

    if direction in ('up', 'both'):
        result['uplink'] = {
            'tx_power_dbm': uplink_tx,
            'rx_power_dbm': uplink_rx,
            'link_loss_dbm': round(uplink_tx - uplink_rx, 2),
            'rx_status': 'degraded' if uplink_rx < -25 else 'normal'
        }

    # 判断整体链路状态
    if direction == 'both':
        if result['downlink']['rx_status'] == 'degraded' or result['uplink']['rx_status'] == 'degraded':
            result['link_status'] = 'degraded'
        else:
            result['link_status'] = 'normal'
    elif direction == 'down':
        result['link_status'] = result['downlink']['rx_status']
    else:
        result['link_status'] = result['uplink']['rx_status']

    return result