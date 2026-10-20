-- Run only against this new, isolated SQ database after its initial schema
-- initialization. Set login credentials separately without printing them.
UPDATE sq_config SET config_value='1' WHERE config_key='system.download.num';
UPDATE sq_config SET config_value='/music' WHERE config_key='system.download.path';
UPDATE sq_config SET config_value='${taskId}/${artists}/${album}/${musicName} - ${artists}' WHERE config_key='system.download.file.template';
UPDATE sq_config SET config_value='flac' WHERE config_key='system.download.file.audio.format';
UPDATE sq_config SET config_value='false' WHERE config_key IN (
 'system.download.failed.use.other.plugin',
 'system.download.degrade',
 'plug.qqvip.sync.my.like.music', 'plug.qqvip.sync.my.like.album',
 'plug.qqvip.sync.my.like.artists', 'plug.qqvip.sync.my.like.playlist',
 'plug.kg.sync.my.collect.playlist', 'plug.kg.sign.open',
 'expand.aliyun.open', 'plug.kg.open', 'plug.kw.open',
 'plug.mg.open', 'plug.freemp3.open', 'plug.tidal.open');
UPDATE sq_config SET config_value='0' WHERE config_key='system.download.timeout.retry.num';
UPDATE sq_config SET config_value='http://sq-neteasecloudmusicapi:3000' WHERE config_key='plug.netease.baseurl';
UPDATE sq_config SET config_value='auto' WHERE config_key IN ('plug.kw.tag.source', 'plug.netease.tag.source', 'plug.qqvip.tag.source');
-- Official anonymous download returned no URL; the project's native extension
-- was verified as a complete FLAC (205.42 seconds, 44.1 kHz) on this NAS.
UPDATE sq_config SET config_value='true' WHERE config_key='plug.netease.extend.download';
