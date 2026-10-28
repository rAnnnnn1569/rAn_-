import argparse
import hashlib
import time
from datetime import datetime
from urllib.parse import quote


def md5_upper(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest().upper()


def build_base(base, scheme="https"):
    base = base.rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = f"{scheme}://{base}"
    return base.rstrip("/")


def build_params(user, pwd, plate, color, t):
    s = md5_upper(md5_upper(pwd) + str(t))
    return (
        f"u={quote(user)}&s={s}&t={t}"
        f"&platenum={quote(plate)}&colorcode={color}"
    )


def build_realplay(base, user, pwd, plate, color, channel, maxchannelnum):
    t = int(time.time() * 1000)
    q = build_params(user, pwd, plate, color, t)
    if channel:
        q += f"&channel={channel}"
    if maxchannelnum:
        q += f"&maxchannelnum={maxchannelnum}"
    return f"{build_base(base, 'http')}/topjtbvideo/realplay?{q}"


def build_history(base, user, pwd, plate, color, allowdownload, singleplay):
    t = int(time.time() * 1000)
    q = build_params(user, pwd, plate, color, t)
    if allowdownload:
        q += f"&allowdownload={allowdownload}"
    if singleplay:
        q += f"&singleplay={singleplay}"
    return f"{build_base(base, 'http')}/historyvideo/playback?{q}"


def build_alarm(base, user, pwd, plate, color, alarmtype, alarmflag, alarmtime):
    t = int(time.time() * 1000)
    q = build_params(user, pwd, plate, color, t)
    if alarmtype:
        q += f"&alarmtype={alarmtype}"
    if alarmflag:
        q += f"&alarmflag={alarmflag}"
    if alarmtime:
        q += f"&alarmtime={alarmtime}"
    return f"{build_base(base, 'http')}/safeanly/alarmattach?{q}"


if __name__ == "__main__":
    now = datetime.now().strftime("%Y%m%d%H%M%S")
    parser = argparse.ArgumentParser(description="生成车安达三种 URL")
    parser.add_argument("--base", default="bd.zwbds.com:8088/cgo8", help="平台地址")
    parser.add_argument("--user", default="gxdsfjg_zf", help="用户名")
    parser.add_argument("--pwd", default="CHANGE_ME", help="用户密码")
    parser.add_argument("--plate", default="桂LB7178", help="车牌号")
    parser.add_argument("--color", default="2", help="颜色编码")
    parser.add_argument("--channel", default="", help="播放通道(实时)，如 1,2")
    parser.add_argument("--maxchannelnum", default="", help="通道最大值(实时)")
    parser.add_argument("--allowdownload", default="true", help="是否允许下载(历史)")
    parser.add_argument("--singleplay", default="false", help="单路播放(历史)")
    parser.add_argument("--alarmtype", default="1", help="报警类别(附件)")
    parser.add_argument("--alarmflag", default="1", help="报警标志(附件)")
    parser.add_argument("--alarmtime", default=now, help=f"报警时间 yyyyMMddHHmmss(附件)，默认{now}")
    args = parser.parse_args()

    print("实时播放:")
    print(build_realplay(args.base, args.user, args.pwd, args.plate,
                         args.color, args.channel, args.maxchannelnum))
    print()
    print("历史回放:")
    print(build_history(args.base, args.user, args.pwd, args.plate,
                        args.color, args.allowdownload, args.singleplay))
    print()
    print("报警附件:")
    print(build_alarm(args.base, args.user, args.pwd, args.plate,
                      args.color, args.alarmtype, args.alarmflag, args.alarmtime))