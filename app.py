import sqlite3
import pandas as pd
import io
import re
from PIL import Image, ImageOps, ImageEnhance, ImageDraw, ImageFont
import pytesseract
from pytesseract import Output
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler

BOT_TOKEN = "8270137357:AAESpC_GzMrE4fjdRyi9kD_-7lwHo_Ztw0w"

FEE_RATE = 0.0005
REBATE_RATE = 0.8

# Pending confirmations: {user_id: trade_data_dict}
pending_confirmations = {}

# 初始化数据库
conn = sqlite3.connect("trades.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT,
    side TEXT,
    entry REAL,
    exit REAL,
    qty REAL,
    pnl REAL,
    fee REAL,
    rebate REAL,
    real_profit REAL,
    time TEXT,
    trade_time TEXT,
    created_at TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS balance_ops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    op_type TEXT, -- INITIAL, DEPOSIT, WITHDRAWAL
    amount REAL,
    created_at TEXT
)
""")

# Check and migrate schema
cursor.execute("PRAGMA table_info(trades)")
columns = [col[1] for col in cursor.fetchall()]
if 'trade_time' not in columns:
    cursor.execute("ALTER TABLE trades ADD COLUMN trade_time TEXT")
    cursor.execute("UPDATE trades SET trade_time = time")
if 'created_at' not in columns:
    cursor.execute("ALTER TABLE trades ADD COLUMN created_at TEXT")
conn.commit()


def get_balance_stats(user_id=None):
    """
    Calculate balance stats.
    Since trades are global (no user_id), we assume the bot represents one portfolio.
    However, balance_ops has user_id to prevent multiple INITIALs per user.
    We will aggregate ALL balance_ops for the calculation if we assume single-tenant usage,
    OR filter by user_id if we want user-specific.
    
    Given the requirement "One account accessing the bot can only set it once",
    it implies user-specific checks.
    
    But if trades are global, mixing user balances is weird.
    Assumption: The bot is used by one person (the admin).
    We will just sum up all balance ops and all trades.
    """
    
    # 1. Get Balance Ops
    cursor.execute("SELECT op_type, amount FROM balance_ops")
    ops = cursor.fetchall()
    
    initial_balance = 0.0
    total_deposit = 0.0
    total_withdrawal = 0.0
    
    for op_type, amount in ops:
        if op_type == 'INITIAL':
            initial_balance += amount
        elif op_type == 'DEPOSIT':
            total_deposit += amount
        elif op_type == 'WITHDRAWAL':
            total_withdrawal += amount
            
    # 2. Get Total Realized Profit from Trades
    cursor.execute("SELECT SUM(real_profit) FROM trades")
    result = cursor.fetchone()
    total_profit = result[0] if result[0] else 0.0
    
    current_balance = initial_balance + total_deposit - total_withdrawal + total_profit
    
    return {
        "initial": initial_balance,
        "deposit": total_deposit,
        "withdrawal": total_withdrawal,
        "profit": total_profit,
        "current": current_balance
    }


def calc_profit(side, entry, exit, qty):
    if side == "多":
        pnl = qty * (exit - entry)
    else:
        pnl = qty * (entry - exit)

    fee = qty * entry * FEE_RATE + qty * exit * FEE_RATE
    rebate = fee * REBATE_RATE
    real_profit = pnl - fee + rebate

    return pnl, fee, rebate, real_profit


def get_stats():
    cursor.execute("SELECT real_profit, trade_time FROM trades")
    rows = cursor.fetchall()

    today = datetime.now().strftime("%Y-%m-%d")
    month = datetime.now().strftime("%Y-%m")

    today_sum = 0
    month_sum = 0
    total_sum = 0

    for profit, t in rows:
        total_sum += profit
        if t.startswith(today):
            today_sum += profit
        if t.startswith(month):
            month_sum += profit

    return today_sum, month_sum, total_sum


def get_settlement_date(trade_dt):
    """
    Get the settlement date (Trading Day) for a given trade time.
    Cycle: 08:00 AM to 08:00 AM next day.
    Example: Jan 11 07:59 -> Jan 10 Trading Day.
             Jan 11 08:01 -> Jan 11 Trading Day.
    """
    if isinstance(trade_dt, str):
        trade_dt = datetime.strptime(trade_dt, "%Y-%m-%d %H:%M:%S")
    
    # Subtract 8 hours to shift 8:00 AM to 0:00 AM
    adjusted = trade_dt - timedelta(hours=8)
    return adjusted.strftime("%Y-%m-%d")


def check_duplicate(symbol, side, entry, exit, qty, trade_time):
    """
    Check if an identical trade exists in the database.
    """
    # Allow small float tolerance if needed, but "completely same" usually means exact matches for manually entered data.
    # For OCR, floats might have small diffs, but usually we parse them to specific values.
    # Let's use a small epsilon for floats.
    
    query = """
    SELECT id FROM trades 
    WHERE symbol = ? 
    AND side = ? 
    AND abs(entry - ?) < 0.0001 
    AND abs(exit - ?) < 0.0001 
    AND abs(qty - ?) < 0.0001
    AND trade_time = ?
    """
    cursor.execute(query, (symbol, side, entry, exit, qty, trade_time))
    return cursor.fetchone() is not None


async def save_trade_to_db(update: Update, trade_data: dict):
    """
    Helper to save trade data to DB and reply to user.
    """
    try:
        symbol = trade_data['symbol']
        side = trade_data['side']
        entry = trade_data['entry']
        exit = trade_data['exit']
        qty = trade_data['qty']
        pnl = trade_data['pnl']
        fee = trade_data['fee']
        rebate = trade_data['rebate']
        real_profit = trade_data['real_profit']
        trade_time = trade_data['trade_time']
        created_at = trade_data['created_at']
        
        cursor.execute("""
        INSERT INTO trades(symbol, side, entry, exit, qty, pnl, fee, rebate, real_profit, trade_time, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (symbol, side, entry, exit, qty, pnl, fee, rebate, real_profit, trade_time, created_at))
        conn.commit()

        today_sum, month_sum, total_sum = get_stats()
        
        # Determine if it's an update object or callback query object
        message = update.message if update.message else update.callback_query.message

        msg = f"""
📊 交易记录成功

标的：{symbol}
方向：{side}
入场价：{entry}
出场价：{exit}
数量：{qty}

合约盈利：{pnl:.4f}
手续费：{fee:.4f}
返佣：{rebate:.4f}
实际盈利：{real_profit:.4f}

📅 今日收益：{today_sum:.2f}
📆 本月收益：{month_sum:.2f}
💰 累计收益：{total_sum:.2f}
⏰ 交易时间：{trade_time}
"""
        await message.reply_text(msg)
        
    except Exception as e:
        print(f"Error saving trade: {e}")
        # Try to reply
        target = update.message if update.message else update.callback_query.message
        await target.reply_text(f"❌ 保存失败: {e}")


async def process_trade_data(update: Update, context: ContextTypes.DEFAULT_TYPE, trade_data: dict):
    """
    Process trade data: Check duplicate -> Confirm or Save.
    """
    symbol = trade_data['symbol']
    side = trade_data['side']
    entry = trade_data['entry']
    exit = trade_data['exit']
    qty = trade_data['qty']
    trade_time = trade_data['trade_time']
    
    if check_duplicate(symbol, side, entry, exit, qty, trade_time):
        user_id = update.effective_user.id
        pending_confirmations[user_id] = trade_data
        
        keyboard = [
            [
                InlineKeyboardButton("✅ 确认插入", callback_data="confirm_yes"),
                InlineKeyboardButton("❌ 取消", callback_data="confirm_no"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        msg = f"""
⚠️ **检测到重复交易记录**

标的：{symbol}
方向：{side}
入场价：{entry}
出场价：{exit}
数量：{qty}
时间：{trade_time}

数据库中已存在完全相同的记录。是否继续插入？
"""
        await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=reply_markup)
    else:
        await save_trade_to_db(update, trade_data)


async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    data = query.data
    
    if user_id not in pending_confirmations:
        await query.edit_message_text(text="❌ 操作已过期或无效。")
        return
        
    if data == "confirm_yes":
        trade_data = pending_confirmations.pop(user_id)
        await query.edit_message_text(text="✅ 正在插入重复记录...")
        await save_trade_to_db(update, trade_data)
        
    elif data == "confirm_no":
        pending_confirmations.pop(user_id)
        await query.edit_message_text(text="❌ 已取消插入。")


async def trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        args = context.args
        if len(args) < 7:
            await update.message.reply_text("❌ 格式错误，请使用：/trade 2026-01-11 14:52:41 eth 多 3090.4 3094.2 0.64")
            return
        time_str = args[0] + " " + args[1]
        m = re.match(r'^(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s+(\d{1,2}):(\d{2}):(\d{2})$', time_str)
        if not m:
            await update.message.reply_text("❌ 时间格式错误，请使用：YYYY-MM-DD HH:MM:SS 或 YYYY/M/D HH:MM:SS")
            return
        y, mo, d, hh, mm, ss = map(int, m.groups())
        trade_time = f"{y:04d}-{mo:02d}-{d:02d} {hh:02d}:{mm:02d}:{ss:02d}"
        symbol = args[2].upper()
        side = args[3]
        entry = float(args[4].replace(",", ""))
        exit = float(args[5].replace(",", ""))
        qty = float(args[6].replace(",", ""))
        pnl, fee, rebate, real_profit = calc_profit(side, entry, exit, qty)
        
        trade_data = {
            "symbol": symbol,
            "side": side,
            "entry": entry,
            "exit": exit,
            "qty": qty,
            "pnl": pnl,
            "fee": fee,
            "rebate": rebate,
            "real_profit": real_profit,
            "trade_time": trade_time,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        
        await process_trade_data(update, context, trade_data)

    except Exception as e:
        await update.message.reply_text("❌ 格式错误，请使用：/trade eth 多 3090 3100 0.5")


async def batch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = update.message.text
        if text.startswith("/batch"):
            text = text[6:]
        
        lines = text.strip().split("\n")
        success = 0
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for line in lines:
            parts = line.strip().split()
            if len(parts) < 7:
                continue
            time_str = parts[0] + " " + parts[1]
            m = re.match(r'^(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s+(\d{1,2}):(\d{2}):(\d{2})$', time_str)
            if not m:
                continue
            y, mo, d, hh, mm, ss = map(int, m.groups())
            trade_time = f"{y:04d}-{mo:02d}-{d:02d} {hh:02d}:{mm:02d}:{ss:02d}"
            symbol = parts[2]
            side = parts[3]
            entry = float(parts[4].replace(",", ""))
            exit = float(parts[5].replace(",", ""))
            qty = float(parts[6].replace(",", ""))
            pnl, fee, rebate, real_profit = calc_profit(side, entry, exit, qty)
            cursor.execute("""
            INSERT INTO trades(symbol, side, entry, exit, qty, pnl, fee, rebate, real_profit, trade_time, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (symbol.upper(), side, entry, exit, qty, pnl, fee, rebate, real_profit, trade_time, now))
            conn.commit()
            success += 1

        today_sum, month_sum, total_sum = get_stats()

        msg = f"""
📥 批量导入完成

成功导入：{success} 笔

📅 今日收益：{today_sum:.2f}
📆 本月收益：{month_sum:.2f}
💰 累计收益：{total_sum:.2f}
⏰ 交易时间：{now}
"""
        await update.message.reply_text(msg)

    except Exception as e:
        print(f"Error in batch: {e}")
        await update.message.reply_text("❌ 批量导入格式错误")


async def clear_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        cursor.execute("DELETE FROM trades")
        cursor.execute("DELETE FROM sqlite_sequence WHERE name='trades'") # Reset ID
        conn.commit()
        await update.message.reply_text("🗑️ 所有交易记录已清空")
    except Exception as e:
        await update.message.reply_text(f"❌ 清空失败: {e}")


async def query_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        print(f"Query request received: {update.message.text}")
        args = context.args
        period = "day"
        if args:
            period = args[0].lower()

        now = datetime.now()
        start_date = now.strftime("%Y-%m-%d")
        
        if period == "week":
            # Start of the week (Monday)
            start_date = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
            period_name = "本周"
        elif period == "month":
            start_date = now.strftime("%Y-%m")
            period_name = "本月"
        else:
            period_name = "今日"

        # SQL Query
        if period == "month":
            query = "SELECT pnl, fee, rebate, real_profit FROM trades WHERE trade_time LIKE ?"
            params = (f"{start_date}%",)
        else:
            query = "SELECT pnl, fee, rebate, real_profit FROM trades WHERE trade_time >= ?"
            params = (f"{start_date} 00:00:00",)

        cursor.execute(query, params)
        rows = cursor.fetchall()

        total_pnl = 0
        total_fee = 0
        total_rebate = 0
        total_real_profit = 0

        for r_pnl, r_fee, r_rebate, r_real in rows:
            total_pnl += r_pnl
            total_fee += r_fee
            total_rebate += r_rebate
            total_real_profit += r_real
        
        # Contract profit usually implies PnL - Fee (Net PnL from exchange perspective before rebate)
        # But user asked for "Contract Profit (i.e. profit after deducting fees)"
        contract_profit_net = total_pnl - total_fee

        # Get Balance Stats
        bal_stats = get_balance_stats()

        msg = f"""
📈 {period_name}收益统计 ({start_date} 至今)

💰 合约净盈亏：{contract_profit_net:.4f} (已扣手续费)
💸 手续费返佣：{total_rebate:.4f}
🏆 实际总盈利：{total_real_profit:.4f}

💎 **当前账户余额：{bal_stats['current']:.2f}**
        """
        await update.message.reply_text(msg)

    except Exception as e:
        await update.message.reply_text(f"❌ 查询出错: {e}")


async def export_excel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # Load data into DataFrame
        df = pd.read_sql_query("SELECT id, symbol, side, entry, exit, qty, pnl, fee, rebate, real_profit, trade_time, created_at FROM trades", conn)
        
        if df.empty:
            await update.message.reply_text("⚠️ 暂无交易记录")
            return

        # Rename columns for better readability
        df.columns = ['ID', '标的', '方向', '入场价', '出场价', '数量', '合约盈亏', '手续费', '返佣', '实际盈利', '交易时间', '创建时间']
        
        # Sort by time
        df.sort_values(by='交易时间', ascending=True, inplace=True)

        # Create Excel file in memory
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            # Sheet 1: All Trades
            df.to_excel(writer, index=False, sheet_name='交易记录')
            
            # Sheet 2: Daily Summary
            # Use Settlement Date (8am-8am)
            df_daily = df.copy()
            # Apply get_settlement_date to '交易时间' column
            df_daily['日期'] = df_daily['交易时间'].apply(get_settlement_date)
            
            # Group by Date
            daily_grp = df_daily.groupby('日期')
            
            # Sum numeric columns
            daily_stats = daily_grp[['合约盈亏', '手续费', '返佣', '实际盈利']].sum()
            
            # Add Counts and Win Rate
            daily_stats['交易笔数'] = daily_grp['ID'].count()
            daily_stats['胜场'] = daily_grp['实际盈利'].apply(lambda x: (x > 0).sum())
            daily_stats['胜率%'] = (daily_stats['胜场'] / daily_stats['交易笔数'] * 100).round(2)
            
            # Calculate Net Contract PnL (pnl - fee)
            daily_stats['合约净盈亏'] = daily_stats['合约盈亏'] - daily_stats['手续费']
            
            # Reorder columns
            cols = ['合约净盈亏', '手续费', '返佣', '实际盈利', '胜率%', '交易笔数']
            daily_stats = daily_stats[cols]
            
            daily_stats.to_excel(writer, sheet_name='按天汇总(结算日)')
            
            # Sheet 3: Equity Curve Data
            # We want cumulative sum of real profit over time
            df_equity = df[['交易时间', '实际盈利']].copy()
            df_equity['累计盈利'] = df_equity['实际盈利'].cumsum()
            df_equity.to_excel(writer, index=False, sheet_name='资金曲线数据')
            
            # Sheet 4: Win Rate Stats
            total_trades = len(df)
            wins = (df['实际盈利'] > 0).sum()
            losses = (df['实际盈利'] < 0).sum()
            breakeven = total_trades - wins - losses
            win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
            
            stats_data = {
                '项目': ['总交易笔数', '盈利笔数', '亏损笔数', '持平笔数', '总胜率%'],
                '数值': [total_trades, wins, losses, breakeven, round(win_rate, 2)]
            }
            pd.DataFrame(stats_data).to_excel(writer, index=False, sheet_name='胜率统计')

        output.seek(0)
        
        filename = f"trades_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        await update.message.reply_document(document=output, filename=filename, caption="📊 您的交易记录已导出 (按结算日汇总)")

    except Exception as e:
        await update.message.reply_text(f"❌ 导出出错: {e}")


async def list_trades(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        args = context.args
        limit = 10
        if args:
            try:
                limit = int(args[0])
            except ValueError:
                pass # default to 10

        cursor.execute("SELECT id, symbol, side, real_profit, trade_time FROM trades ORDER BY trade_time DESC LIMIT ?", (limit,))
        rows = cursor.fetchall()

        if not rows:
            await update.message.reply_text("📭 暂无交易记录")
            return

        msg = f"📋 最近 {len(rows)} 笔交易记录：\n\n"
        for r in rows:
            # r: (id, symbol, side, real_profit, time)
            tid, symbol, side, profit, time_str = r
            # Format time slightly shorter: MM-DD HH:MM
            try:
                dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
                short_time = dt.strftime("%m-%d %H:%M")
            except:
                short_time = time_str
            
            icon = "🟢" if profit >= 0 else "🔴"
            msg += f"{icon} `#{tid}` {symbol} {side} **{profit:.2f}** ({short_time})\n"
        
        msg += "\n🔍 查看详情：`/view ID`\n🗑️ 删除记录：`/delete ID`"
        await update.message.reply_text(msg, parse_mode='Markdown')

    except Exception as e:
        await update.message.reply_text(f"❌ 获取列表失败: {e}")


async def view_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        args = context.args
        if not args:
            await update.message.reply_text("请指定交易ID，例如：/view 1")
            return
        
        tid = args[0]
        cursor.execute("SELECT id, symbol, side, entry, exit, qty, pnl, fee, rebate, real_profit, trade_time FROM trades WHERE id = ?", (tid,))
        row = cursor.fetchone()
        
        if not row:
            await update.message.reply_text(f"❌ 未找到 ID 为 {tid} 的交易")
            return

        # Schema: id, symbol, side, entry, exit, qty, pnl, fee, rebate, real_profit, trade_time
        (tid, symbol, side, entry, exit, qty, pnl, fee, rebate, real_profit, time_str) = row
        
        msg = f"""
🔍 **交易详情 #{tid}**

标的：{symbol}
方向：{side}
入场价：{entry}
出场价：{exit}
数量：{qty}

合约盈亏：{pnl:.4f}
手续费：{fee:.4f}
返佣：{rebate:.4f}
实际盈利：{real_profit:.4f}

⏰ 时间：{time_str}
"""
        await update.message.reply_text(msg, parse_mode='Markdown')

    except Exception as e:
        await update.message.reply_text(f"❌ 获取详情失败: {e}")


async def delete_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        args = context.args
        if not args:
            await update.message.reply_text("请指定要删除的交易ID，例如：/delete 1")
            return
        
        tid = args[0]
        
        # Check if exists
        cursor.execute("SELECT id FROM trades WHERE id = ?", (tid,))
        if not cursor.fetchone():
            await update.message.reply_text(f"❌ 未找到 ID 为 {tid} 的交易")
            return

        cursor.execute("DELETE FROM trades WHERE id = ?", (tid,))
        conn.commit()
        
        await update.message.reply_text(f"✅ 已删除交易 #{tid}")

    except Exception as e:
        await update.message.reply_text(f"❌ 删除失败: {e}")


async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_text("🖼️ 正在识别截图...")
        
        photo = update.message.photo[-1]
        file = await photo.get_file()
        byte_array = await file.download_as_bytearray()
        
        image = Image.open(io.BytesIO(byte_array))
        
        # Preprocessing: Grayscale + Scale Up + Contrast
        image = image.convert('L')
        width, height = image.size
        image = image.resize((width * 2, height * 2), Image.Resampling.LANCZOS)
        enhancer = ImageEnhance.Contrast(image)
        image = enhancer.enhance(2.0)

        # OCR
        # Try Chinese + English
        # Use image_to_data for layout analysis (Primary)
        print("Attempting Layout Analysis (Position-based)...")
        layout_data = parse_with_layout(image)
        
        if layout_data:
            print(f"Layout Analysis Result: {layout_data}")
            symbol, side, entry, exit, qty, trade_time = layout_data
            # Recalculate PnL/Fee/Rebate based on these trusted values
            pnl, fee, rebate, real_profit = calc_profit(side, entry, exit, qty)
            
            # Use found time or current
            if trade_time:
                now = trade_time
            else:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                
            # Save
            cursor.execute("""
            INSERT INTO trades(symbol, side, entry, exit, qty, pnl, fee, rebate, real_profit, trade_time, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (symbol.upper(), side, entry, exit, qty, pnl, fee, rebate, real_profit, now, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()
            
            today_sum, month_sum, total_sum = get_stats()
            
            msg = f"""
✅ 截图识别并记录成功 (布局分析)

标的：{symbol.upper()}
方向：{side}
入场价：{entry}
出场价：{exit}
数量：{qty}

合约盈利：{pnl:.4f}
手续费：{fee:.4f}
返佣：{rebate:.4f}
实际盈利：{real_profit:.4f}

📅 今日收益：{today_sum:.2f}
📆 本月收益：{month_sum:.2f}
💰 累计收益：{total_sum:.2f}
⏰ 交易时间：{now}
"""
            await update.message.reply_text(msg)
            return

        # Fallback to Text-based Parsing (Old Logic)
        text = pytesseract.image_to_string(image, lang='chi_sim+eng')
        print(f"OCR Result (Text):\n{text}") # Debug log
        
        # Parse
        data = parse_screenshot_text(text)
        
        if not data:
            # Try only English if Chinese fails or mix causes issues (sometimes helps)
            print("Retrying with English only...")
            text_eng = pytesseract.image_to_string(image, lang='eng')
            print(f"OCR Result (Eng):\n{text_eng}")
            data = parse_screenshot_text(text_eng)

        if not data:
            await update.message.reply_text(f"❌ 无法识别交易详情。\n识别到的文本片段：\n{text[:100]}...")
            return

        symbol, side, entry, exit, qty, trade_time = data
        
        # Calculate
        pnl, fee, rebate, real_profit = calc_profit(side, entry, exit, qty)
        
        if trade_time:
            # Use extracted time
            now = trade_time
        else:
            # Fallback to current time
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        trade_data = {
            "symbol": symbol.upper(),
            "side": side,
            "entry": entry,
            "exit": exit,
            "qty": qty,
            "pnl": pnl,
            "fee": fee,
            "rebate": rebate,
            "real_profit": real_profit,
            "trade_time": now,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        
        await process_trade_data(update, context, trade_data)

    except Exception as e:
        await update.message.reply_text(f"❌ 图片处理出错: {e}")


def extract_time(text):
    # Try to find time associated with "平仓时间" (Exit Time)
    # Pattern: 平仓时间 followed by date
    match = re.search(r'(?:平仓时间|Close Time|Time)[^\d]*?(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1)
        
    # If not found, look for any date-time.
    # Usually the last one is the exit time.
    matches = re.findall(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', text)
    if matches:
        return matches[-1]
        
    return None


def parse_screenshot_text(text):
    # Normalize text
    text = text.replace(",", "") # Remove commas in numbers
    
    # Extract time first
    trade_time = extract_time(text)
    
    # 1. Find Symbol (e.g., ETHUSDT)
    symbol_match = re.search(r'([A-Z]+)USDT', text)
    if not symbol_match:
        return None
    symbol = symbol_match.group(1)
    
    # 2. Find Side (平多 -> 多, 平空 -> 空)
    side = None
    if "平多" in text:
        side = "多"
    elif "平空" in text or "平室" in text: # Handle common OCR error for 平空
        side = "空"
    
    # If not found, maybe check English (Close Long / Close Short)
    if not side:
        if "Close Long" in text or "Buy" in text: 
             side = "多"
        elif "Close Short" in text or "Sell" in text:
             side = "空"
    
    # 3. Find Entry Price
    # Pattern: 开仓均价 (USDT) 3318.91
    entry_match = re.search(r'(?:开仓均价|Entry|Avg Price)[^\d]*?(\d+\.?\d*)', text, re.IGNORECASE | re.DOTALL)
    entry = None
    if entry_match:
        entry = float(entry_match.group(1))

    # 4. Find Exit Price
    # Pattern: 平仓均价 (USDT) 3317.00
    exit_match = re.search(r'(?:平仓均价|Exit)[^\d]*?(\d+\.?\d*)', text, re.IGNORECASE | re.DOTALL)
    exit_price = None
    if exit_match:
        exit_price = float(exit_match.group(1))

    # 5. Find Qty
    # Pattern: 平仓数量 (ETH) 0.60
    # Use [^\d]*? to match anything (including newlines) until the first digit
    qty_match = re.search(r'(?:数量|Qty)[^\d]*?(\d+\.?\d*)', text, re.IGNORECASE | re.DOTALL)
    qty = None
    if qty_match:
        val = float(qty_match.group(1))
        # Avoid picking up Entry/Exit as Qty if they are close
        if (entry and abs(val - entry) < 0.1) or (exit_price and abs(val - exit_price) < 0.1):
             # Try next match?
             # Simple regex search only finds first.
             # We can try findall?
             qty_matches = re.findall(r'(?:数量|Qty)[^\d]*?(\d+\.?\d*)', text, re.IGNORECASE | re.DOTALL)
             for m in qty_matches:
                 v = float(m)
                 if (entry and abs(v - entry) < 0.1) or (exit_price and abs(v - exit_price) < 0.1):
                     continue
                 qty = v
                 break
        else:
             qty = val

    # 6. Find PnL (Realized Profit)
    # Pattern: 平仓盈亏 (USDT) -10.3462
    pnl_match = re.search(r'(?:平仓盈亏|Realized PnL)[^\d-]*?(-?\d+\.?\d*)', text, re.IGNORECASE | re.DOTALL)
    pnl = None
    if pnl_match:
        pnl = float(pnl_match.group(1))

    # 7. Find ROI
    roi_match = re.search(r'(?:收益率|ROI)[^\d-]*?(-?\d+\.?\d*)%', text, re.IGNORECASE | re.DOTALL)
    if not roi_match:
         roi_match = re.search(r'(-?\d+\.?\d*)%', text)
    roi = float(roi_match.group(1)) if roi_match else None

    # Fallback: If critical data missing, try heuristic
    # Don't return None early if Side is missing, as we might infer it in heuristic or via PnL/ROI
    
    # Infer Side if missing but we have prices and PnL/ROI
    if not side and entry and exit_price:
        diff = exit_price - entry
        if diff != 0:
             # Strategy 1: Use PnL
             if pnl is not None:
                 if (pnl > 0 and diff > 0) or (pnl < 0 and diff < 0):
                     side = "多"
                 else:
                     side = "空"
             # Strategy 2: Use ROI
             elif roi is not None:
                 if (roi > 0 and diff > 0) or (roi < 0 and diff < 0):
                     side = "多"
                 else:
                     side = "空"
                     print(f"Inferred Side: {side} based on ROI {roi} and Price Diff {diff}")

    # If side is still missing, we will try to infer it in heuristic if possible, or return None later.

    # Sanity Check
    is_suspicious = False
    
    # Check if side is missing
    if not side:
        is_suspicious = True
        print("Side missing, triggering heuristic/inference...")

    if entry and exit_price and entry > 0 and exit_price > 0:
        price_diff_ratio = abs(entry - exit_price) / max(entry, exit_price)
        if price_diff_ratio > 0.8: 
            print(f"Suspicious price difference: {entry} vs {exit_price}")
            is_suspicious = True
            
    if qty and qty > 0:
        # Check collision with entry/exit (ignoring small diffs)
        # Note: entry/exit could be None if not found by regex
        if (entry and abs(entry - qty) < 0.0001) or (exit_price and abs(exit_price - qty) < 0.0001):
              print(f"Suspicious collision")
              is_suspicious = True
    
    # Also check if critical values are missing
    if not (qty and entry and exit_price and side):
        is_suspicious = True

    if is_suspicious:
        print("Regex results deemed suspicious or incomplete. Attempting heuristic parsing...")
        # Pass regex findings to help heuristic
        heuristic_data = parse_heuristic(text, symbol, side, roi, entry, exit_price)
        if heuristic_data:
             return heuristic_data + (trade_time,)
             
    if not side:
        return None
        
    return symbol, side, entry, exit_price, qty, trade_time


def parse_with_layout(image):
    """
    Use pytesseract image_to_data to find values based on spatial position (layout).
    This avoids regex confusion when multiple numbers are present.
    Strategy: Find "Label", then look for "Value" directly below it (same column).
    """
    try:
        d = pytesseract.image_to_data(image, output_type=Output.DICT, lang='chi_sim+eng')
        n_boxes = len(d['level'])
        
        # 1. Reconstruct Lines/Words with coordinates
        # We group by (block_num, par_num, line_num)
        # But for simplicity, let's just iterate and find keywords.
        # Since keywords might be split (e.g. "开", "仓"), we need to search for sequences.
        
        # Or simpler: Just find ANY word that matches part of the keyword, 
        # check if neighbors match the rest?
        # Even simpler: Just look for the unique parts.
        # "均价" (Avg Price) is unique enough.
        # "数量" (Qty) is unique enough.
        
        # Helper to find a number below a given label box
        def find_number_below(label_box, max_y_dist=200):
            # label_box: (x, y, w, h)
            lx, ly, lw, lh = label_box
            l_center_x = lx + lw / 2
            l_bottom_y = ly + lh
            
            candidates = []
            for i in range(n_boxes):
                if d['text'][i].strip() == '': continue
                
                # Check if it's a number
                # Remove commas, %
                val_str = d['text'][i].replace(',', '').replace('%', '')
                try:
                    val = float(val_str)
                except ValueError:
                    continue
                
                # Check position
                x, y, w, h = d['left'][i], d['top'][i], d['width'][i], d['height'][i]
                center_x = x + w / 2
                
                # Horizontal alignment: center within label width (expanded slightly)
                # Allow some margin (e.g. 50% of label width)
                margin = max(lw, w) * 0.8
                if abs(center_x - l_center_x) > margin:
                    continue
                
                # Vertical alignment: Below label
                if y > l_bottom_y and y < l_bottom_y + max_y_dist:
                    candidates.append((val, y))
            
            # Sort by Y (top to bottom), pick closest
            candidates.sort(key=lambda x: x[1])
            if candidates:
                return candidates[0][0]
            return None

        # Helper to find label box
        def find_label_box(keywords):
            # Find a line containing the keyword (or sequence of words)
            # Reconstruct lines first
            lines = {} # key: (block, par, line) -> list of indices
            for i in range(n_boxes):
                if d['text'][i].strip() == '': continue
                key = (d['block_num'][i], d['par_num'][i], d['line_num'][i])
                if key not in lines: lines[key] = []
                lines[key].append(i)
            
            for key, indices in lines.items():
                line_text = "".join([d['text'][i] for i in indices])
                
                for kw in keywords:
                    if kw in line_text:
                        # Found line with keyword. 
                        # Ideally we want the box of JUST the keyword, but the line box is often good enough for column alignment 
                        # if the keyword takes up most of the column width or we align by center of the matching words.
                        
                        # Let's try to find the specific words that make up the keyword?
                        # Too complex for now. Let's use the bounding box of the matching words in the line.
                        
                        # Find start/end index of match in the concatenated string? Hard because spaces are gone.
                        # Simple approach: Use the bounding box of the WHOLE line?
                        # No, because "Qty (ETH)   Entry (USDT)" might be one line!
                        # If we use whole line center, it will be in the middle of page.
                        
                        # We need to find which WORDS in the line correspond to the keyword.
                        # Since we joined without spaces, let's just iterate words and try to match?
                        
                        # Greedy match:
                        current_text = ""
                        start_idx = 0
                        
                        # This is getting complicated to do perfectly.
                        # Alternative: Just look for the specific unique sub-word.
                        # e.g. "数量" might be d['text'][i] == "数量" or "数" + "量"
                        
                        # Let's iterate words in the line.
                        for i in range(len(indices)):
                            idx = indices[i]
                            word = d['text'][idx]
                            
                            # Check if this word IS the keyword or part of it?
                            # If we search for "数量", and word is "数量", great.
                            # If word is "平仓数量", great.
                            if kw in word:
                                return (d['left'][idx], d['top'][idx], d['width'][idx], d['height'][idx])
                            
                            # What if split? "数" "量"
                            # If we see "数", check next word "量"?
                            if len(kw) > 1 and word == kw[0]:
                                # Check next
                                if i + 1 < len(indices):
                                    next_idx = indices[i+1]
                                    if d['text'][next_idx] == kw[1]:
                                        # Union box
                                        l = min(d['left'][idx], d['left'][next_idx])
                                        t = min(d['top'][idx], d['top'][next_idx])
                                        r = max(d['left'][idx] + d['width'][idx], d['left'][next_idx] + d['width'][next_idx])
                                        b = max(d['top'][idx] + d['height'][idx], d['top'][next_idx] + d['height'][next_idx])
                                        return (l, t, r-l, b-t)
                        
                        # If we failed to find exact word match, but line has it...
                        # Maybe it's "平仓数量(ETH)" as one word? handled by `kw in word`.
                        
            return None

        # 2. Extract Data
        # Symbol
        symbol = None
        for i in range(n_boxes):
            txt = d['text'][i]
            if "USDT" in txt and len(txt) > 4:
                # e.g. ETHUSDT
                symbol = txt.replace("USDT", "")
                break
        
        # Side
        side = None
        # Scan full text for Side keywords
        full_text = "".join(d['text'])
        if "平多" in full_text or "CloseLong" in full_text.replace(" ", ""):
            side = "多"
        elif "平空" in full_text or "平室" in full_text or "CloseShort" in full_text.replace(" ", ""):
            side = "空"
            
        # ROI for side inference
        roi = None
        for i in range(n_boxes):
             if "%" in d['text'][i]:
                 try:
                     val = float(d['text'][i].replace('%', '').replace('+', ''))
                     roi = val
                 except: pass
        
        # Qty
        # Keywords: "数量", "Qty"
        qty_box = find_label_box(["数量", "Qty"])
        qty = find_number_below(qty_box) if qty_box else None
        
        # Entry
        # Keywords: "开仓均价", "Entry", "Avg Price"
        # Note: "开仓均价" is unique.
        entry_box = find_label_box(["开仓均价", "Entry", "AvgPrice"])
        entry = find_number_below(entry_box) if entry_box else None
        
        # Exit
        # Keywords: "平仓均价", "Exit"
        exit_box = find_label_box(["平仓均价", "Exit", "Price"]) # "Price" is risky?
        exit_price = find_number_below(exit_box) if exit_box else None
        
        # Time
        trade_time = extract_time(" ".join(d['text'])) # Use existing regex on joined text
        
        print(f"Layout Debug: Symbol={symbol}, Side={side}, Qty={qty}, Entry={entry}, Exit={exit_price}")
        
        # Side Inference
        if not side and entry and exit_price and roi:
             diff = exit_price - entry
             if diff != 0:
                 if (roi > 0 and diff > 0) or (roi < 0 and diff < 0):
                     side = "多"
                 else:
                     side = "空"
        
        if symbol and (side or roi) and qty and entry and exit_price:
            if not side: side = "多" # Fallback if inferred failed but we have everything else?
            return symbol, side, entry, exit_price, qty, trade_time
            
        return None
        
    except Exception as e:
        print(f"Layout Parsing Error: {e}")
        return None


def parse_heuristic(text, symbol, side, roi=None, regex_entry=None, regex_exit=None):
    # Extract all numbers
    # Remove dates first to avoid confusion
    text_clean = re.sub(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', '', text)
    text_clean = re.sub(r'\d{4}-\d{2}-\d{2}', '', text_clean) # Extra date removal
    text_clean = re.sub(r'\d{2}:\d{2}:\d{2}', '', text_clean) # Extra time removal
    
    # Pre-filtering: Remove numbers associated with Fees or Margin
    # These often confuse the logic, especially if Qty or PnL is small
    
    # 1. Fees (手续费)
    fee_matches = re.findall(r'(?:手续费|Fee)[^\d]*?(\d+(?:,\d{3})*\.\d+)', text_clean)
    fees = [float(n.replace(',', '')) for n in fee_matches]
    
    # 2. Margin (保证金)
    margin_matches = re.findall(r'(?:保证金|Margin)[^\d]*?(\d+(?:,\d{3})*\.\d+)', text_clean)
    margins = [float(n.replace(',', '')) for n in margin_matches]
    
    print(f"Heuristic Filter: Ignoring Fees={fees}, Margins={margins}")
    
    # Find all floats
    numbers = re.findall(r'(\d+(?:,\d{3})*\.\d+)', text_clean)
    numbers = [float(n.replace(',', '')) for n in numbers]
    
    # Filter numbers
    filtered_numbers = []
    for n in numbers:
        # Check if it matches any fee or margin (with tolerance)
        is_ignored = False
        for ignored in fees + margins:
            if abs(n - ignored) < 0.0001:
                is_ignored = True
                break
        if not is_ignored:
            filtered_numbers.append(n)
            
    numbers = filtered_numbers
    
    # Filter out percentages (if they were captured as numbers)
    # The regex \d+\.\d+ might capture 24.59 from 24.59%
    # We should look at original text positions, but list filtering is easier.
    # Percentages are usually ROI, so they might be large (100%) or small.
    # Let's keep them for now.
    
    # NEW: Filter out ROI if we have it
    if roi is not None:
         filtered_numbers_roi = []
         for n in numbers:
             if abs(n - abs(roi)) > 0.001: # Use abs(roi) because regex matches number part
                 filtered_numbers_roi.append(n)
         numbers = filtered_numbers_roi
         print(f"Heuristic Filter: Removed ROI {roi}, Remaining: {numbers}")
    
    if len(numbers) < 3:
        return None
    
    # Sort numbers descending
    numbers.sort(reverse=True)
    print(f"Heuristic Numbers: {numbers}")
    
    # Assumption: The two largest numbers are Entry and Exit Prices (for Crypto like ETH/BTC)
    # This might fail for low value coins (XRP < 1.0), but works for ETH/BTC.
    price1 = numbers[0]
    price2 = numbers[1]
    
    # Remaining numbers
    others = numbers[2:]
    
    # Determine Entry vs Exit based on Side
    # Long (多): Profit > 0 implies Exit > Entry. Loss implies Exit < Entry.
    # We don't know if it's profit or loss yet, but usually users post profits.
    # Let's check if we can find a matching PnL.
    
    diff = abs(price1 - price2)
    if diff == 0:
        return None
        
    found_qty = None
    found_pnl = None
    
    # Try to find a number X in others such that X / diff is a "clean" number (Qty)
    # OR X * diff matches another number (PnL)
    
    for x in others:
        # Hypothesis 1: x is PnL
        qty_candidate = x / diff
        # Check if qty_candidate looks like a round number (e.g. 0.64, 0.5, 1.0, 0.01)
        # Allow small error margin
        
        # Check if this qty_candidate exists in others (as the Qty field)
        for y in others:
            if x == y: continue
            if abs(y - qty_candidate) < 0.001:
                 found_pnl = x
                 found_qty = y
                 print(f"Heuristic Match: PnL={found_pnl}, Qty={found_qty} (found in list)")
                 break
        
        if found_qty: break
        
        # Also check if it is a "clean" number even if not in list (sometimes Qty is not parsed as number due to OCR noise)
        # e.g. 0.640000000000046 -> 0.64
        if abs(qty_candidate - round(qty_candidate, 4)) < 0.0001:
             # But wait, PnL = Qty * Diff is strictly true only for linear contracts.
             # If PnL matches 3rd largest number, it's a strong signal.
             pass

    if not found_qty:
        # Hypothesis 2: x is Qty
        for x in others:
            pnl_candidate = x * diff
            # Check if pnl_candidate exists in others (roughly)
            for y in others:
                if x == y: continue
                # Allow larger error for PnL calculation as exchange logic might vary (fees etc)
                # But here we assume Gross PnL ~ Qty * Diff
                # If Exchange shows Net PnL, this might fail.
                # However, usually there is "Realized PnL" which is Net?
                # Let's try matching with 0.1 tolerance?
                if abs(y - pnl_candidate) < 0.05:
                    found_qty = x
                    found_pnl = y
                    print(f"Heuristic Match: Qty={found_qty}, PnL={found_pnl} (verified)")
                    break
            if found_qty: break
            
    # If still not found, but we have a number that "looks like" PnL (e.g. 3rd largest)
    # and we can't find Qty. We might just assume Qty = PnL / Diff.
    if not found_qty and len(others) > 0:
        # Take the largest remaining number as PnL (risky but often PnL > Fee)
        # Unless Fee is very high?
        # In the user example: PnL=2.432, Fee=0.99. PnL > Fee.
        # So assume 3rd largest is PnL.
        
        # IMPROVEMENT: Check if any remaining number matches ROI logic?
        # PnL = Initial Margin * ROI? 
        # Margin = Entry * Qty / Leverage.
        # Too complex.
        
        # Fallback: Assume largest remaining is PnL?
        # In user case: others=[0.89, 0.89]. Largest is 0.89.
        # Diff = 1.0. Qty = 0.89 / 1.0 = 0.89.
        # This matches!
        
        # But wait, previous error was: Assumed PnL=5.99 (ROI), Derived Qty=5.99.
        # Because 5.99 was in the list!
        # Now we removed 5.99. List is [0.89, 0.89].
        
        found_pnl = others[0]
        if diff > 0:
            found_qty = found_pnl / diff
        else:
             # Should not happen as diff is abs? No, diff is defined as pinned_exit - pinned_entry earlier?
             # No, earlier `diff = abs(price1 - price2)`
             found_qty = 0 # Error
             
        # Check if found_qty matches another number in list?
        # If others has duplicates (0.89, 0.89), it's a strong sign one is Qty and one is PnL.
        if len(others) >= 2 and abs(others[0] - others[1]) < 0.001:
             # Case where PnL approx equals Qty (happens when price diff is ~1.0)
             found_qty = others[0] # or others[1]
             print(f"Heuristic: Found duplicate numbers {found_qty}, assuming Qty=PnL.")
        else:
             print(f"Heuristic Fallback: Assumed PnL={found_pnl}, Derived Qty={found_qty}")
    
    if not found_qty:
        return None
        
    # Check for negative ROI to determine if it's a Loss
    # If roi passed in is None, check regex again (redundant but safe)
    is_loss = False
    if roi is not None:
         if roi < 0:
             is_loss = True
             print(f"Using passed ROI: {roi}%, Loss={is_loss}")
    else:
        roi_match = re.search(r'(-?\d+\.?\d*)%', text)
        if roi_match:
            roi = float(roi_match.group(1))
            if roi < 0:
                is_loss = True
                print(f"Detected negative ROI in heuristic: {roi}%, assuming Loss.")

    # Infer Side if missing
    if not side:
        # Determine Entry/Exit using regex hints
        # We have price1 and price2 (sorted descending, so price1 > price2)
        
        pinned_entry = None
        pinned_exit = None
        
        # Check if regex matches
        # Allow 1.0 tolerance for float/rounding diffs
        if regex_entry:
            if abs(regex_entry - price1) < 1:
                pinned_entry = price1
            elif abs(regex_entry - price2) < 1:
                pinned_entry = price2
                
        if regex_exit:
            if abs(regex_exit - price1) < 1:
                pinned_exit = price1
            elif abs(regex_exit - price2) < 1:
                pinned_exit = price2
        
        # Deduce the other
        if pinned_exit and not pinned_entry:
            pinned_entry = price2 if pinned_exit == price1 else price1
        elif pinned_entry and not pinned_exit:
            pinned_exit = price2 if pinned_entry == price1 else price1
            
        # If we successfully identified both (or inferred one from the other)
        if pinned_entry and pinned_exit:
             diff = pinned_exit - pinned_entry
             # Use ROI to determine Side
             if roi is not None and diff != 0:
                 if (roi > 0 and diff > 0) or (roi < 0 and diff < 0):
                     side = "多"
                 else:
                     side = "空"
                 print(f"Heuristic Inferred Side: {side} (ROI={roi}, Diff={diff})")
                 
        # If still no side, but we found Qty and PnL...
        # Maybe PnL sign? found_pnl is absolute value in my logic?
        # No, found_pnl comes from numbers list, which are float(n).
        # regex `(\d+(?:,\d{3})*\.\d+)` does NOT capture negative sign!
        # So found_pnl is always positive.
        # So we can't use PnL sign unless we re-check text for negative sign.
        # But ROI usually has negative sign inside text?
        # `roi_match` captures `(-?...)`. Yes.
        
        # If we can't infer side, default to "空" (Short) as a fallback? 
        # Or better, just fail?
        if not side:
             print("Heuristic failed to infer side.")
             # Fallback: Assume Short if we have 'FS' or similar? No.
             return None

    # Assign Entry/Exit
    # Price1 is larger, Price2 is smaller
    high_price = max(price1, price2)
    low_price = min(price1, price2)
    
    if side == "多":
        # Long Profit: Exit > Entry (High > Low)
        # Long Loss: Exit < Entry (Low > High)
        if not is_loss:
            exit_price = high_price
            entry_price = low_price
        else:
            exit_price = low_price
            entry_price = high_price
            
    else: # 空
        # Short Profit: Entry > Exit (High > Low)
        # Short Loss: Entry < Exit (Low > High)
        if not is_loss:
            entry_price = high_price
            exit_price = low_price
        else:
            entry_price = low_price
            exit_price = high_price
            
    return symbol, side, entry_price, exit_price, found_qty


async def reindex(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # Create temp table with auto-increment ID
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS trades_tmp (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            side TEXT,
            entry REAL,
            exit REAL,
            qty REAL,
            pnl REAL,
            fee REAL,
            rebate REAL,
            real_profit REAL,
            trade_time DATETIME NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)
        
        # Check if trade_time exists in source (it should, based on schema check)
        # We copy data ordered by trade_time
        # Note: We do NOT copy 'id', letting it auto-increment
        # We map columns explicitly to be safe
        cursor.execute("DELETE FROM trades_tmp")
        
        # We need to make sure we select columns that exist. 
        # The schema in 'start' has: id, symbol, side, entry, exit, qty, pnl, fee, rebate, real_profit, time, trade_time, created_at
        # We ignore 'time' (legacy) and use 'trade_time'.
        
        cursor.execute("""
        INSERT INTO trades_tmp(symbol, side, entry, exit, qty, pnl, fee, rebate, real_profit, trade_time, created_at)
        SELECT symbol, side, entry, exit, qty, pnl, fee, rebate, real_profit, trade_time, created_at
        FROM trades 
        ORDER BY trade_time ASC
        """)
        
        # Drop old and rename new
        cursor.execute("DROP TABLE trades")
        cursor.execute("ALTER TABLE trades_tmp RENAME TO trades")
        
        conn.commit()
        await update.message.reply_text("✅ 已按时间重新排序并重建序号")
        
    except Exception as e:
        await update.message.reply_text(f"❌ 重建失败: {e}")


async def daily_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        cursor.execute("SELECT pnl, fee, rebate, real_profit, trade_time FROM trades ORDER BY trade_time ASC")
        rows = cursor.fetchall()
        
        if not rows:
            await update.message.reply_text("📭 暂无交易记录")
            return

        data = {}
        for pnl, fee, rebate, real_profit, t in rows:
            # t is "YYYY-MM-DD HH:MM:SS"
            settlement_date = get_settlement_date(t)
            
            if settlement_date not in data:
                data[settlement_date] = {"pnl": 0.0, "fee": 0.0, "rebate": 0.0, "real": 0.0, "win": 0, "loss": 0, "count": 0}
            
            d = data[settlement_date]
            d["pnl"] += pnl
            d["fee"] += fee
            d["rebate"] += rebate
            d["real"] += real_profit
            d["count"] += 1
            if real_profit > 0:
                d["win"] += 1
            elif real_profit < 0:
                d["loss"] += 1
        
        days = sorted(data.keys())
        msg = "📅 **按天汇总 (8:00-8:00)**\n\n"
        
        total_real = 0
        for day in days:
            d = data[day]
            net_contract = d["pnl"] - d["fee"]
            win_rate = (d["win"] / d["count"] * 100) if d["count"] > 0 else 0
            msg += f"`{day}` | 净 {net_contract:>6.1f} | 返 {d['rebate']:>5.1f} | 实 {d['real']:>6.1f} | 胜 {win_rate:>4.1f}%\n"
            total_real += d["real"]
            
        msg += f"\n💰 总实际盈利：{total_real:.2f}\n"
        
        # Calculate Pending Rebate
        now = datetime.now()
        today_8am = now.replace(hour=8, minute=0, second=0, microsecond=0)
        today_1330 = now.replace(hour=13, minute=30, second=0, microsecond=0)
        
        if now >= today_1330:
            last_settled_time = today_8am
        else:
            last_settled_time = today_8am - timedelta(days=1)
            
        pending_rebate = 0.0
        for pnl, fee, rebate, real_profit, t in rows:
             dt = datetime.strptime(t, "%Y-%m-%d %H:%M:%S")
             if dt >= last_settled_time:
                 pending_rebate += rebate
                 
        msg += f"\n⏳ 待结算返佣 (预计 12:15 到账): {pending_rebate:.4f}"
        
        await update.message.reply_text(msg, parse_mode='Markdown')
        
    except Exception as e:
        await update.message.reply_text(f"❌ 汇总出错: {e}")


async def equity_curve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        cursor.execute("SELECT real_profit, trade_time FROM trades ORDER BY trade_time ASC")
        rows = cursor.fetchall()
        
        if not rows:
            await update.message.reply_text("📭 暂无交易记录")
            return
            
        points = []
        cum = 0.0
        # Start with 0 at the first date? Or just cumulative trade by trade
        points.append((rows[0][1], 0.0)) # Initial point
        
        for rp, t in rows:
            cum += rp
            points.append((t, cum))
            
        # Draw
        width = 800
        height = 400
        img = Image.new("RGB", (width, height), (255, 255, 255))
        draw = ImageDraw.Draw(img)
        
        # Normalize
        ys = [p[1] for p in points]
        ymin = min(ys)
        ymax = max(ys)
        span = ymax - ymin if ymax != ymin else 1.0
        
        # Margins
        margin_top = 40
        margin_bottom = 40
        margin_left = 60
        margin_right = 20
        plot_h = height - margin_top - margin_bottom
        plot_w = width - margin_left - margin_right
        
        n = len(points)
        
        coords = []
        for i in range(n):
            # X coordinate: equally spaced by trade index (or by time? by trade index is cleaner for "trades")
            x = margin_left + int(i / max(1, n-1) * plot_w)
            
            # Y coordinate
            val = points[i][1]
            # Invert Y (0 is top)
            y = margin_top + plot_h - int((val - ymin) / span * plot_h)
            coords.append((x, y))
            
        # Draw grid
        draw.line([(margin_left, margin_top), (margin_left, height-margin_bottom)], fill=(200,200,200), width=2) # Y axis
        # Zero line if visible
        if ymin <= 0 <= ymax:
            y_zero = margin_top + plot_h - int((0 - ymin) / span * plot_h)
            draw.line([(margin_left, y_zero), (width-margin_right, y_zero)], fill=(255,0,0), width=1)
            
        # Draw line
        for j in range(1, len(coords)):
            draw.line([coords[j-1], coords[j]], fill=(46, 125, 50), width=2)
            
        # Draw stats
        start_val = ys[0]
        end_val = ys[-1]
        draw.text((10, 10), f"Start: {start_val:.2f}", fill=(0,0,0))
        draw.text((10, 25), f"End: {end_val:.2f}", fill=(0,0,0))
        draw.text((10, 40), f"Max: {ymax:.2f}", fill=(0,0,0))
        draw.text((10, 55), f"Min: {ymin:.2f}", fill=(0,0,0))
        
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        
        await update.message.reply_photo(photo=buf, caption="📈 资金曲线 (按交易笔数)")
        
    except Exception as e:
        await update.message.reply_text(f"❌ 生成资金曲线出错: {e}")


async def winrate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        cursor.execute("SELECT real_profit, trade_time FROM trades ORDER BY trade_time ASC")
        rows = cursor.fetchall()
        
        if not rows:
            await update.message.reply_text("📭 暂无交易记录")
            return
            
        total_count = len(rows)
        win_count = sum(1 for r in rows if r[0] > 0)
        loss_count = sum(1 for r in rows if r[0] < 0)
        breakeven_count = total_count - win_count - loss_count
        
        win_rate = win_count / total_count * 100
        
        # By Symbol
        cursor.execute("SELECT symbol, real_profit FROM trades")
        rows_sym = cursor.fetchall()
        stats_sym = {}
        for s, p in rows_sym:
            if s not in stats_sym: stats_sym[s] = {"w":0, "l":0, "c":0, "p":0.0}
            stats_sym[s]["c"] += 1
            stats_sym[s]["p"] += p
            if p > 0: stats_sym[s]["w"] += 1
            elif p < 0: stats_sym[s]["l"] += 1
            
        msg = f"""
🏆 **胜率统计**

总交易：{total_count} 笔
✅ 盈利：{win_count} 笔
❌ 亏损：{loss_count} 笔
⚖️ 持平：{breakeven_count} 笔
🔥 **胜率：{win_rate:.2f}%**

📊 **按币种统计**
"""
        for s, d in stats_sym.items():
            wr = (d["w"]/d["c"]*100) if d["c"]>0 else 0
            msg += f"- {s}: {wr:.1f}% ({d['w']}/{d['c']}) 💰 {d['p']:.2f}\n"
            
        await update.message.reply_text(msg, parse_mode='Markdown')
        
    except Exception as e:
        await update.message.reply_text(f"❌ 统计失败: {e}")


async def init_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        args = context.args
        if not args:
            await update.message.reply_text("❌ 请输入初始金额，例如：/init 10000")
            return
            
        amount = float(args[0])
        
        # Check if already initialized for this user
        cursor.execute("SELECT id FROM balance_ops WHERE user_id = ? AND op_type = 'INITIAL'", (user_id,))
        if cursor.fetchone():
            await update.message.reply_text("❌ 您已设置过初始金额，无法重复设置。请使用 /deposit 或 /withdraw 进行调整。")
            return
            
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("INSERT INTO balance_ops (user_id, op_type, amount, created_at) VALUES (?, 'INITIAL', ?, ?)", 
                       (user_id, amount, now))
        conn.commit()
        
        await update.message.reply_text(f"✅ 初始金额已设置为：{amount:.2f}")
        
    except ValueError:
        await update.message.reply_text("❌ 金额格式错误")
    except Exception as e:
        await update.message.reply_text(f"❌ 设置失败: {e}")


async def deposit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        args = context.args
        if not args:
            await update.message.reply_text("❌ 请输入充值金额，例如：/deposit 5000")
            return
            
        amount = float(args[0])
        if amount <= 0:
            await update.message.reply_text("❌ 金额必须大于 0")
            return
            
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("INSERT INTO balance_ops (user_id, op_type, amount, created_at) VALUES (?, 'DEPOSIT', ?, ?)", 
                       (user_id, amount, now))
        conn.commit()
        
        stats = get_balance_stats()
        await update.message.reply_text(f"✅ 已增加资金：{amount:.2f}\n当前余额：{stats['current']:.2f}")
        
    except ValueError:
        await update.message.reply_text("❌ 金额格式错误")
    except Exception as e:
        await update.message.reply_text(f"❌ 操作失败: {e}")


async def withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        args = context.args
        if not args:
            await update.message.reply_text("❌ 请输入提取金额，例如：/withdraw 2000")
            return
            
        amount = float(args[0])
        if amount <= 0:
            await update.message.reply_text("❌ 金额必须大于 0")
            return
            
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("INSERT INTO balance_ops (user_id, op_type, amount, created_at) VALUES (?, 'WITHDRAWAL', ?, ?)", 
                       (user_id, amount, now))
        conn.commit()
        
        stats = get_balance_stats()
        await update.message.reply_text(f"✅ 已减少资金：{amount:.2f}\n当前余额：{stats['current']:.2f}")
        
    except ValueError:
        await update.message.reply_text("❌ 金额格式错误")
    except Exception as e:
        await update.message.reply_text(f"❌ 操作失败: {e}")


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        stats = get_balance_stats()
        
        msg = f"""
💰 **资金账户概览**

🏁 初始本金：{stats['initial']:.2f}
📥 累计入金：{stats['deposit']:.2f}
📤 累计出金：{stats['withdrawal']:.2f}
📈 累计盈亏：{stats['profit']:.2f}

💎 **当前余额：{stats['current']:.2f}**
"""
        await update.message.reply_text(msg, parse_mode='Markdown')
        
    except Exception as e:
        await update.message.reply_text(f"❌ 查询失败: {e}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("""
欢迎使用合约交易记账机器人 📊

指令格式：
/trade 日期 时间 标的 多/空 入场价 出场价 数量

资金管理：
/init <金额> - 设置初始本金 (仅一次)
/deposit <金额> - 资金转入 (入金)
/withdraw <金额> - 资金转出 (出金)
/balance - 查看资金账户详情

高级功能：
/query [day|week|month] - 查询收益与余额
/list [n] - 查看最近 n 笔交易 (默认10)
/view <ID> - 查看交易详情
/delete <ID> - 删除指定交易
/reindex - 按时间重排序并修复序号
/daily - 查看按天汇总报表
/equity - 查看资金曲线图
/winrate - 查看胜率统计
/export - 导出 Excel 交易记录 (含分析)
🖼️ 发送交易截图 - 自动识别并记账

示例：
/trade 2026-01-11 14:52:41 eth 多 3090.4 3094.2 0.64
/query week
""")


if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("trade", trade))
    app.add_handler(CommandHandler("batch", batch))
    app.add_handler(CommandHandler("clear", clear_data))
    app.add_handler(CommandHandler("query", query_stats))
    app.add_handler(CommandHandler("list", list_trades))
    app.add_handler(CommandHandler("view", view_trade))
    app.add_handler(CommandHandler("delete", delete_trade))
    app.add_handler(CommandHandler("reindex", reindex))
    app.add_handler(CommandHandler("daily", daily_report))
    app.add_handler(CommandHandler("equity", equity_curve))
    app.add_handler(CommandHandler("winrate", winrate))
    app.add_handler(CommandHandler("export", export_excel))
    
    # Balance Commands
    app.add_handler(CommandHandler("init", init_balance))
    app.add_handler(CommandHandler("deposit", deposit))
    app.add_handler(CommandHandler("withdraw", withdraw))
    app.add_handler(CommandHandler("balance", balance))
    
    app.add_handler(MessageHandler(filters.PHOTO, handle_image))
    
    # Callback Query Handler for Confirmations
    app.add_handler(CallbackQueryHandler(confirm_callback))

    print("🤖 交易机器人已启动...")
    app.run_polling()
