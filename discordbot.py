import discord
import asyncio
import psutil
import requests
import ssl
import os

# ===============================================
# 🔑 1. 設定情報 (環境変数から読み込むように修正) 🔑
# ===============================================
# Discord Botのトークンを環境変数から取得
# 環境変数が見つからない場合、プログラムを停止させる（セキュリティのため）
DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN')
if not DISCORD_TOKEN:
    raise ValueError("DISCORD_TOKEN 環境変数が設定されていません。systemdファイルを確認してください。")

# Proxmox VE API 設定を環境変数から取得
PROXMOX_HOST = os.environ.get('PROXMOX_HOST')
PROXMOX_TOKEN_ID = os.environ.get('PROXMOX_TOKEN_ID')
PROXMOX_TOKEN_SECRET = os.environ.get('PROXMOX_TOKEN_SECRET')

# Proxmox APIが使用するポート。通常は8006
PROXMOX_PORT = 8006
# Proxmoxノード名 (デフォルトでは "pve" など。ホスト名を確認してください)
PROXMOX_NODE = 'pve-core01'

# ===============================================
# 🤖 2. Discordクライアントのセットアップ 🤖
# ===============================================
# メッセージの内容を読むためのインテントを有効化
intents = discord.Intents.default()
intents.message_content = True 

client = discord.Client(intents=intents)

# -----------------------------------------------
# Proxmox API 連携関数
# -----------------------------------------------

def get_proxmox_host_status():
    """Proxmox APIからホストのCPU, メモリ情報を取得する"""
    
    # Proxmoxはデフォルトで自己署名証明書を使うため、証明書検証を無効にする設定
    # ⚠️ 警告: 実運用ではセキュリティリスクを伴うため、証明書を設定することを推奨
    ssl._create_default_https_context = ssl._create_unverified_context
    
    # API URL
    url = f"https://{PROXMOX_HOST}:{PROXMOX_PORT}/api2/json/nodes/{PROXMOX_NODE}/status"
    headers = {
        "Authorization": f"PVEAPIToken={PROXMOX_TOKEN_ID}={PROXMOX_TOKEN_SECRET}"
    }

    try:
        response = requests.get(url, headers=headers, verify=False, timeout=10) 
        response.raise_for_status() # HTTPエラーがあれば例外を発生させる
        
        data = response.json().get('data', {})
        
        # 取得したデータから情報を抽出
        cpu_usage = round(data.get('cpu', 0.0) * 100, 1)
        
        # メモリ情報をGB単位で計算
        mem_total_gb = data.get('memory', {}).get('total', 0) / (1024**3)
        mem_used_gb = data.get('memory', {}).get('used', 0) / (1024**3)
        
        return {
            'cpu_usage': cpu_usage,
            'mem_used': mem_used_gb,
            'mem_total': mem_total_gb,
            'status': 'OK'
        }
    except requests.exceptions.RequestException as e:
        print(f"Proxmox APIエラー: {e}")
        return {'status': 'Error', 'error_message': f'API接続エラー: {e.__class__.__name__}'}

# -----------------------------------------------
# 定期実行タスク (リッチプレゼンス更新)
# -----------------------------------------------

async def update_rich_presence():
    """Botのステータス（アクティビティ）を定期的に更新する"""
    await client.wait_until_ready() # Botが完全に起動するまで待機
    
    while not client.is_closed():
        # 1. Proxmoxのステータスを取得
        status_data = get_proxmox_host_status()
        
        if status_data['status'] == 'OK':
            cpu = status_data['cpu_usage']
            mem_used = status_data['mem_used']
            mem_total = status_data['mem_total']
            
            # ステータス文字列を作成
            activity_name = f"CPU: {cpu}% | RAM: {mem_used:.1f}G/{mem_total:.1f}G" 
            status = discord.Status.online
        else:
            # エラー時はエラーメッセージを表示
            activity_name = f"🚨 ステータス取得エラー ({status_data['error_message']})"
            status = discord.Status.dnd # Do Not Disturb (取り込み中)
            
        # 2. Discordのステータスを設定（リッチプレゼンス）
        activity = discord.Game(name=activity_name) 
        await client.change_presence(activity=activity, status=status)
        
        # 3. 60秒待ってから次の更新を行う
        await asyncio.sleep(60) 

# ===============================================
# 🔔 3. Discordイベントハンドラ 🔔
# ===============================================

@client.event
async def on_ready():
    """BotがDiscordに接続したときに実行される"""
    print('----------------------------------------')
    print(f'ログインしました: {client.user}') 
    print(f'Node: {PROXMOX_HOST}, Token: {PROXMOX_TOKEN_ID}')
    print('----------------------------------------')
    
    # 起動時に定期実行タスクを開始
    client.loop.create_task(update_rich_presence()) 


@client.event
async def on_message(message):
    """メッセージが送信されたときに実行される"""
    # Bot自身のメッセージには反応しない
    if message.author == client.user:
        return

    # 簡易応答
    if message.content.startswith('こんにちは'):
        await message.channel.send(f'{message.author.display_name}さん、こんにちは！')
        
    # ステータス応答
    if message.content.startswith('!ステータス'):
        # 処理中であることをユーザーに伝える
        await message.channel.send("自宅サーバーのステータスを取得中です...少々お待ちください。")
        
        status_data = get_proxmox_host_status()
        
        if status_data['status'] == 'OK':
            # 情報を整形して表示
            status_message = (
                f"**🏡 Proxmox VE ホスト ステータス**\n"
                f"--- CPU ---\n"
                f"使用率: **{status_data['cpu_usage']}%**\n"
                f"--- メモリ ---\n"
                f"合計: {status_data['mem_total']:.2f} GB\n"
                f"使用済: {status_data['mem_used']:.2f} GB\n"
                f"--- 稼働中VM/LXC ---\n"
                f"※ VM/LXCの個別の状態取得は別途実装が必要です。"
            )
        else:
            status_message = f"🚨 **ステータス取得エラー:** Proxmox APIに接続できませんでした。\n詳細: `{status_data['error_message']}`"
            
        await message.channel.send(status_message)


# ===============================================
# 🚀 4. Botの実行 🚀
# ===============================================
if __name__ == "__main__":
    if DISCORD_TOKEN == 'ここに取得したDiscord Botトークンを貼り付ける' or not DISCORD_TOKEN:
        print("エラー: Discord Botトークンが設定されていません。コードの1行目を修正してください。")
    else:
        try:
            client.run(DISCORD_TOKEN) 
        except discord.errors.LoginFailure:
            print("エラー: トークンが無効です。Discord Developer Portalで確認してください。")
        except Exception as e:
            print(f"Bot実行中に予期せぬエラーが発生しました: {e}")