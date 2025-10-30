import os
import requests
import discord
import asyncio
import json
from datetime import datetime
from io import BytesIO
import matplotlib.pyplot as plt

# ===============================================
# 環境変数
# ===============================================
PROXMOX_API = f"https://{os.environ['PROXMOX_HOST']}:8006/api2/json"
PROXMOX_TOKEN_ID = os.environ['PROXMOX_TOKEN_ID']
PROXMOX_TOKEN_SECRET = os.environ['PROXMOX_TOKEN_SECRET']
DISCORD_TOKEN = os.environ['DISCORD_TOKEN']

# JSONファイル保存先（logsディレクトリ）
LOG_DIR = "/root/discord-bot/logs"
os.makedirs(LOG_DIR, exist_ok=True)
CONFIG_FILE = os.path.join(LOG_DIR, "notify_config.json")
HISTORY_FILE = os.path.join(LOG_DIR, "status_history.json")

INTERVAL = 60  # 定期監視間隔(秒)

# 管理者用秘密ワード（緊急用全体表示）
EMERGENCY_WORD = "emerg12345"

# ===============================================
# Discord Bot設定
# ===============================================
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# グローバル変数
previous_status = {}
notify_config = {"nodes": {}, "vms": {}}  # JSONで永続化
history_data = {}  # 過去状態

# ===============================================
# JSON読み書き
# ===============================================
def load_json(file_path, default):
    if os.path.exists(file_path):
        with open(file_path, "r") as f:
            return json.load(f)
    return default

def save_json(file_path, data):
    with open(file_path, "w") as f:
        json.dump(data, f, indent=2)

notify_config = load_json(CONFIG_FILE, notify_config)
history_data = load_json(HISTORY_FILE, history_data)

# ===============================================
# Proxmox API
# ===============================================
def get_proxmox_session():
    session = requests.Session()
    session.headers.update({
        "Authorization": f"PVEAPIToken={PROXMOX_TOKEN_ID}={PROXMOX_TOKEN_SECRET}"
    })
    # systemdで設定した REQUESTS_CA_BUNDLE を使う（なければ通常の検証）
    verify_path = os.getenv("REQUESTS_CA_BUNDLE")
    if verify_path:
        session.verify = verify_path
    return session

def get_node_status(session):
    res = session.get(f"{PROXMOX_API}/nodes", timeout=10)
    res.raise_for_status()
    nodes = res.json().get("data", [])

    detailed = []
    for n in nodes:
        detailed.append({
            "node": n["node"],
            "status": n["status"].upper(),
            "cpu": n["cpu"],
            "mem": n["mem"],
            "maxmem": n["maxmem"],
        })
    return detailed

def get_vm_status(session):
    res = session.get(f"{PROXMOX_API}/cluster/resources", verify=True)
    res.raise_for_status()
    # VMだけ抽出
    return [v for v in res.json()["data"] if v["type"] == "qemu"]

# ===============================================
# 表示用フォーマット
# ===============================================
def format_summary(nodes, vms):
    summary = []
    for node in nodes:
        status = node['status'].upper()
        icon = "🟢" if status=="RUNNING" else "🔴" if status=="STOPPED" else "⚪"
        summary.append(f"{node['node']}{icon}")
    for vm in vms:
        status = vm['status'].upper()
        icon = "🟢" if status=="RUNNING" else "🔴" if status=="STOPPED" else "⚪"
        summary.append(f"{vm['name']}{icon}")
    return " | ".join(summary)

def format_detail(nodes, vms):
    lines = ["🖥 **ノード詳細**"]
    for node in nodes:
        status = node['status']
        icon = "🟢" if status.startswith("RUNNING") else "🔴" if status.startswith("STOPPED") else "⚪"
        cpu_pct = node['cpu'] * 100
        mem_pct = (node['mem'] / node['maxmem'] * 100) if node['maxmem'] else 0
        lines.append(f"- {node['node']}: {status} {icon} (CPU: {cpu_pct:.1f}% / MEM: {mem_pct:.1f}%)")

    lines.append("\n**VM詳細**")
    for vm in vms:
        status = vm['status'].upper()
        icon = "🟢" if status=="RUNNING" else "🔴" if status=="STOPPED" else "⚪"
        cpu_pct = vm.get('cpu', 0) * 100
        mem_pct = (vm.get('mem', 0) / vm.get('maxmem', 1) * 100) if vm.get('maxmem') else 0
        lines.append(f"- {vm['name']}({vm['vmid']}): {status} {icon} (CPU: {cpu_pct:.1f}% / MEM: {mem_pct:.1f}%)")
    return "\n".join(lines)

# ===============================================
# グラフ生成
# ===============================================
def generate_graph(target_name):
    """過去データからグラフ生成"""
    times, cpu_vals, mem_vals = [], [], []

    if target_name not in history_data:
        return None

    for entry in history_data[target_name]:
        times.append(datetime.fromisoformat(entry['time']))
        cpu_vals.append(entry['cpu'])
        mem_vals.append(entry['mem'])

    plt.figure(figsize=(6,3))
    plt.plot(times, cpu_vals, label='CPU', color='blue')
    plt.plot(times, mem_vals, label='MEM', color='green')
    plt.title(target_name)
    plt.xlabel("Time")
    plt.ylabel("Usage")
    plt.legend()
    plt.tight_layout()

    buf = BytesIO()
    plt.savefig(buf, format='png')
    buf.seek(0)
    plt.close()
    return buf

# ===============================================
# 定期監視
# ===============================================
async def monitor():
    await client.wait_until_ready()
    global previous_status, notify_config, history_data
    while not client.is_closed():
        try:
            session = get_proxmox_session()
            nodes = get_node_status(session)
            vms = get_vm_status(session)

            # リッチプレゼンス更新
            summary = format_summary(nodes, vms)
            await client.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name=summary))

            # -------------------
            # 履歴保存
            # -------------------
            timestamp = datetime.utcnow().isoformat()
            for node in nodes:
                entry = {"time": timestamp, "cpu": node['cpu'], "mem": node['mem']/node['maxmem']}
                history_data.setdefault(node['node'], []).append(entry)
            for vm in vms:
                entry = {"time": timestamp, "cpu": vm['cpu'], "mem": vm['mem']/vm['maxmem']}
                history_data.setdefault(vm['name'], []).append(entry)
            save_json(HISTORY_FILE, history_data)

            # -------------------
            # 停止通知
            # -------------------
            for node in nodes:
                name = node['node']
                status = node['status'].upper()
                prev = previous_status.get(name)
                if prev and prev != status and status=="STOPPED":
                    channel_id = notify_config['nodes'].get(name)
                    if channel_id:
                        channel = client.get_channel(channel_id)
                        if channel:
                            await channel.send(f"⚠️ ノード `{name}` が停止しました！")
                previous_status[name] = status

            for vm in vms:
                name = vm['name']
                status = vm['status'].upper()
                prev = previous_status.get(name)
                if prev and prev != status and status=="STOPPED":
                    channel_id = notify_config['vms'].get(name)
                    if channel_id:
                        channel = client.get_channel(channel_id)
                        if channel:
                            await channel.send(f"⚠️ VM `{name}`({vm['vmid']}) が停止しました！")
                previous_status[name] = status

        except Exception as e:
            print(f"監視エラー: {e}")

        await asyncio.sleep(INTERVAL)

# ===============================================
# Discordコマンド
# ===============================================
@client.event
async def on_message(message):
    if message.author == client.user:
        return
    content = message.content.strip()
    author_id = message.author.id

    # --------------------
    # !help
    # --------------------
    if content == "!help":
        await message.channel.send(
            "📌 **コマンド一覧**\n"
            "`!status` - 現在のノード・VM詳細を表示\n"
            "`!status-detail <Node名|VM名>` - 過去履歴のグラフを表示（引数必須）\n"
            "`!status-detail-emergency-<秘密ワード>` - 緊急用、全Node/VMのグラフ出力（管理者のみ）\n"
            "`!notify_node <Node名>` / `!unnotify_node <Node名>` - ノード通知設定/解除\n"
            "`!notify_vm <VM名>` / `!unnotify_vm <VM名>` - VM通知設定/解除\n"
            "`!listnotify` - 現在の通知設定一覧"
        )

    # --------------------
    # !status
    # --------------------
    elif content == "!status":
        try:
            session = get_proxmox_session()
            nodes = get_node_status(session)
            vms = get_vm_status(session)
            await message.channel.send(format_detail(nodes, vms))
        except Exception as e:
            await message.channel.send(f"⚠️ ステータス取得エラー: {e}")

    # --------------------
    # !status-detail <name>
    # --------------------
    elif content.startswith("!status-detail "):
        target = content.split(" ",1)[1]
        buf = generate_graph(target)
        if buf:
            await message.channel.send(file=discord.File(buf, filename=f"{target}.png"))
        else:
            await message.channel.send(f"⚠️ {target} の履歴データがありません。")

    # --------------------
    # 緊急用全体グラフ
    # --------------------
    elif content.startswith(f"!status-detail-emergency-{EMERGENCY_WORD}"):
        session = get_proxmox_session()
        nodes = get_node_status(session)
        vms = get_vm_status(session)
        all_targets = [n['node'] for n in nodes] + [v['name'] for v in vms]
        for t in all_targets:
            buf = generate_graph(t)
            if buf:
                await message.channel.send(file=discord.File(buf, filename=f"{t}.png"))

    # --------------------
    # 通知登録系
    # --------------------
    elif content.startswith("!notify_node "):
        name = content.split(" ",1)[1]
        notify_config['nodes'][name] = message.channel.id
        save_json(CONFIG_FILE, notify_config)
        await message.channel.send(f"✅ ノード `{name}` 通知先登録しました。")

    elif content.startswith("!unnotify_node "):
        name = content.split(" ",1)[1]
        if name in notify_config['nodes']:
            notify_config['nodes'].pop(name)
            save_json(CONFIG_FILE, notify_config)
            await message.channel.send(f"✅ ノード `{name}` 通知解除しました。")

    elif content.startswith("!notify_vm "):
        name = content.split(" ",1)[1]
        notify_config['vms'][name] = message.channel.id
        save_json(CONFIG_FILE, notify_config)
        await message.channel.send(f"✅ VM `{name}` 通知先登録しました。")

    elif content.startswith("!unnotify_vm "):
        name = content.split(" ",1)[1]
        if name in notify_config['vms']:
            notify_config['vms'].pop(name)
            save_json(CONFIG_FILE, notify_config)
            await message.channel.send(f"✅ VM `{name}` 通知解除しました。")

    elif content == "!listnotify":
        lines = ["📌 通知設定一覧"]
        for n, c in notify_config['nodes'].items():
            lines.append(f"Node {n}: <#{c}>")
        for v, c in notify_config['vms'].items():
            lines.append(f"VM {v}: <#{c}>")
        await message.channel.send("\n".join(lines))

# ===============================================
# 起動
# ===============================================
@client.event
async def on_ready():
    print(f"✅ ログイン完了: {client.user}")
    client.loop.create_task(monitor())

client.run(DISCORD_TOKEN)
