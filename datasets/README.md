# 固定复现输入

- fossil-src-2.28.tar.gz：官方固定版本源码，原版权声明保留在包内。
- fossil-snapshot.fossil.gz：本次公开Fossil仓库快照；包含本地镜像只读压测配置。
- traces/：训练种子42、正式评估种子43与44，每份100个请求；不是线上日志。
- manifest.json：来源、解压后快照哈希与各输入文件哈希。

使用scripts/init_fossil_reproduction.py还原。不要用重新clone或data命令生成的新快照替代历史输入。
