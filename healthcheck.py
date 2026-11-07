"""Read the configured Jira identity without starting MCP, Web or the worker."""
from __future__ import annotations

import sys

from jira import JIRA, JIRAError
from requests.exceptions import ConnectionError, SSLError, Timeout

from config import JiraConfig, load_config


class JiraHealthcheckError(RuntimeError):
    """A connection-check failure safe to display in the terminal."""


def verify_jira_connection(cfg: JiraConfig) -> None:
    client = None
    try:
        client = JIRA(
            options={"server": cfg.url, "verify": cfg.verify_ssl},
            token_auth=cfg.token if cfg.auth_type == "pat" else None,
            basic_auth=(cfg.username, cfg.password) if cfg.auth_type == "basic" else None,
            timeout=cfg.timeout, get_server_info=False, max_retries=0,
            logging=False,
        )
        user = client.myself()
        if not isinstance(user, dict) or user.get("active") is False or not any(
            isinstance(user.get(key), str)
            and user[key].strip()
            and user[key].strip().lower() != "anonymous"
            for key in ("name", "key", "accountId")
        ):
            raise JiraHealthcheckError("响应中无法确认已认证的 Jira 用户。")
    except JiraHealthcheckError:
        raise
    except JIRAError as exc:
        if exc.status_code == 401:
            message = "Jira 认证失败，请检查认证方式和凭据。"
        elif exc.status_code == 403:
            message = "Jira 拒绝访问，请检查账号权限或认证策略。"
        else:
            message = "Jira 返回异常状态，请检查实例地址和服务状态。"
        raise JiraHealthcheckError(message) from None
    except SSLError:
        raise JiraHealthcheckError("TLS 证书校验失败，请检查证书信任配置。") from None
    except Timeout:
        raise JiraHealthcheckError("Jira 连接超时，请检查网络和超时设置。") from None
    except ConnectionError:
        raise JiraHealthcheckError("无法连接 Jira，请检查地址、网络或 VPN。") from None
    except Exception:
        raise JiraHealthcheckError("Jira 响应或客户端异常，无法确认身份。") from None
    finally:
        if client is not None:
            client.close()


def main() -> int:
    try:
        verify_jira_connection(load_config().jira)
    except (JiraHealthcheckError, OSError, UnicodeError, ValueError) as exc:
        # Configuration errors contain key names only; transport exceptions are sanitized above.
        message = str(exc) if isinstance(exc, (JiraHealthcheckError, ValueError)) and not isinstance(exc, UnicodeError) else "无法读取项目 .env，请运行 setup 并检查文件权限和编码。"
        print(f"Jira 检查失败：{message}", file=sys.stderr)
        return 1
    print("Jira authentication succeeded. 身份认证通过；不代表所有项目均有权限。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
