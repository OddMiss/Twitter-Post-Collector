# Security policy

## Scope

X-Agent is designed for local use. The Python service binds to `127.0.0.1` and uses a random local token so that unrelated local web pages cannot submit requests without the token.

## Do not report secrets

请不要在 Issue 或 Pull Request 中粘贴：

- `OPENAI_API_KEY`；
- `.collector-token` 内容；
- 浏览器 Cookie、profile 或登录信息；
- 未公开的个人帖子数据。

如果发现密钥泄露，请立即在对应服务中撤销/轮换密钥，并通过私下渠道报告，而不是公开提交。

## Reporting a vulnerability

请在 GitHub 私密安全报告中提供：影响范围、复现步骤、受影响的版本、以及不包含秘密的最小证明。若仓库尚未启用私密安全报告，请先联系维护者，再决定是否公开披露。
