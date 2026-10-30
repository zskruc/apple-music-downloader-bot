"""Human-readable fault messages without echoing upstream URLs or secrets."""
LABELS = {
    'transport': '来源连接暂时失败，将按有限退避自动重试。',
    'resource_budget': '本轮处理超过时间预算，将按有限退避自动重试。',
    'permission': '来源拒绝访问，需要恢复账号或权限。',
    'parse': '来源返回的内容无法解析，已停止无效的网络重试并保留诊断。',
    'response_semantics': '来源返回的内容不符合接口要求，已保留诊断。',
    'tls_configuration': '来源证书校验未通过，需要修复连接配置，未关闭证书校验。',
    'playback': '播放器尚未返回与已发布歌词一致的结果。',
    'content_changed': '处理期间文件发生变化，正在按当前文件重新核验。',
    'internal': '处理逻辑遇到未分类异常，诊断已保留，需要检查服务。',

    'network': '来源连接暂时失败，将按有限退避自动重试。',
    'temporary_network': '来源连接暂时失败，将按有限退避自动重试。',
    'authentication': '来源拒绝认证，需要恢复账号或权限。',
    'authorization': '来源拒绝访问，需要恢复账号或权限。',
    'rate_limit': '来源限制请求频率，正在等待允许重试的时间。',
    'provider_response': '来源返回的内容异常，已保留诊断。',
    'provider_semantic': '来源返回的内容不符合要求，已保留诊断。',
    'media_integrity': '音频完整性检查未通过，已保留原文件，需要核对或更换音源。',
    'identity': '候选歌词与录音版本无法可靠对应，已拒绝混用。',
    'no_source': '尚无可信且版本匹配的歌词来源。',
    'player': '播放器尚未返回与已发布歌词一致的结果。',
}

def fault_description(payload):
    state = payload.get('source_state') or {}
    fault = next(iter(payload.get('source_faults') or []), {})
    category = state.get('category') or fault.get('category')
    return LABELS.get(category, '当前条件未满足自动处理要求，已保留明确诊断，未覆盖原音频。')
