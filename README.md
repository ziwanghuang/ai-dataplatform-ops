# ai-dataplatform-ops



## 🚢 推送到远端服务器

使用 rsync 将项目推送到远端服务器，自动排除不需要同步的文件：

```bash

rsync -avz \
    --exclude='.git/' \
    --exclude='.github/' \
    --exclude='__pycache__/' \
    --exclude='.workbuddy' \
    --exclude='venv' \
    /Users/ziwh666/GitHub/ai-dataplatform-ops \
    root@182.43.22.165:/data/github/
```

```bash
git fetch origin && git reset --hard origin/main
```

> 💡 **免密推送**：建议先配置 SSH 密钥认证，执行一次 `ssh-copy-id root@your-server-ip` 即可免密。
