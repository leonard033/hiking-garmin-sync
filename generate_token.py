"""本地生成 Garmin OAuth token。

用法：
    1. 设置环境变量 GARMIN_USER / GARMIN_PASS / GARMIN_IS_CN（可选）
    2. python generate_token.py
    3. 复制输出的一长串 base64 字符，填到 Railway 的 GARMIN_TOKEN 变量里

Token 有效期约 6 个月，过期后重新生成即可。
"""
import os
import getpass
from garminconnect import Garmin


def main():
    email = os.environ.get("GARMIN_USER") or input("Garmin 邮箱: ").strip()
    password = os.environ.get("GARMIN_PASS") or getpass.getpass("Garmin 密码: ")
    is_cn = os.environ.get("GARMIN_IS_CN", "true").lower() in ("true", "1", "yes")

    if not email or not password:
        print("邮箱/密码不能为空")
        return

    print(f"正在登录 Garmin ({'中国区' if is_cn else '国际区'})...")
    garmin = Garmin(email=email, password=password, is_cn=is_cn, prompt_mfa=False)
    garmin.login()

    token_dir = os.path.expanduser("~/.garminconnect")
    garmin.garth.dump(token_dir)
    print(f"Token 已保存到本地: {token_dir}")

    token_base64 = garmin.garth.dumps()
    print("\n======== 请复制下面整段 token，填到 Railway 的 GARMIN_TOKEN 变量里 ========\n")
    print(token_base64)
    print("\n====================================================================")

    try:
        name = garmin.get_full_name()
        print(f"\n验证通过，登录身份: {name}")
    except Exception as e:
        print(f"\n获取用户名失败（可忽略）: {e}")


if __name__ == "__main__":
    main()
