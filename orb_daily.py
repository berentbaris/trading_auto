import time
import pytz
import yfinance as yf
import pandas as pd
from pushbullet import Pushbullet
from datetime import datetime, timedelta
import sys
import os

# === Setup ===
token = os.getenv("PUSHBULLET_TOKEN")
pb = Pushbullet(token)
cet = pytz.timezone("Europe/Amsterdam")
et = pytz.timezone("US/Eastern")
pb.push_note("Script Started", "The trading script is now running.")

# === Functions ===
def get_market_open_close():
    now_cet = datetime.now(cet)
    now_et = now_cet.astimezone(et)
    market_open = now_et.replace(hour=9, minute=45, second=0, microsecond=0)
    market_close = market_open + timedelta(hours=5)
    return market_open, market_close

def fetch_data():
    df = yf.download("QQQ", interval="5m", period="15d")
    vix = yf.download("^VIX", interval="5m", period="15d")['Close']
    xlu = yf.download("XLU", interval="5m", period="15d")['Close']
    vix = vix.dropna()

    df.columns = df.columns.get_level_values(0)
    for series in [df, vix, xlu]:
        series.index = series.index.tz_convert('US/Eastern')

    df = df.between_time('09:30', '16:00').apply(pd.to_numeric, errors='coerce').dropna()
    ema = df['Close'].ewm(span=20).mean().diff()
    ema = ema.between_time('09:30', '16:00')
    vix = vix.between_time('09:30', '16:00')
    xlu = xlu.between_time('09:30', '16:00')

    df = df.join(vix).join(ema.rename('ema_slope')).join(xlu)
    df.rename(columns={'^VIX': 'VIX'}, inplace=True)
    df.reset_index(inplace=True)
    df['date'] = pd.to_datetime(df['Datetime']).dt.tz_localize(None).dt.normalize()
    df.set_index('Datetime', inplace=True)
    df['risk_on_ratio'] = df['Close'] / df['XLU']
    return df

def run_strategy(df):
    pb.push_note("Running strategy", "Started running the strategy.")
    results = []
    unique_dates = sorted(df['date'].unique())
    if len(unique_dates) < 2:
        pb.push_note("Script Stopped", "Error 1")
        sys.exit("Terminating script due to Error 1.")

    today = unique_dates[-1]
    yesterday = unique_dates[-2]

    day_data = df[df['date'] == today]
    if len(day_data) < 1:
        pb.push_note("Script Stopped", "Error 2")
        sys.exit("Terminating script due to Error 2.")

    opening_range = day_data.iloc[:3]
    opening_high, opening_low = opening_range['High'].max(), opening_range['Low'].min()
    opening_close, opening_open = opening_range['Close'].iloc[-1], opening_range['Open'].iloc[-3]
    opening_strength = (opening_close - opening_low) / (opening_high - opening_low)

    rest_of_day = day_data.iloc[3:]
    breakout_up = rest_of_day[rest_of_day['Close'] > opening_high]
    breakout_down = rest_of_day[rest_of_day['Close'] < opening_low]
    first_breakout_up = breakout_up.index[0] if not breakout_up.empty else None
    first_breakout_down = breakout_down.index[0] if not breakout_down.empty else None

    above_orb = below_orb = False
    breakout_up_times = []
    breakout_down_times = []

    for idx, row in rest_of_day.iterrows():
        close = row['Close']

        # Long breakout detection
        if not above_orb and close > opening_high:
            # First breakout
            above_orb = True
            breakout_up_times.append(idx)

        elif above_orb and close <= opening_high:
            # Retest complete, ready for next breakout
            above_orb = False

        # Short breakout detection
        if not below_orb and close < opening_low:
            below_orb = True
            breakout_down_times.append(idx)

        elif below_orb and close >= opening_low:
            below_orb = False

    vix_avg = opening_range['VIX'].mean()
    ema_avg = opening_range['ema_slope'].mean()
    ror_avg = opening_range['risk_on_ratio'].mean()

    prev_idx = df.index.get_loc(opening_range.index[0]) - 1
    vix_prev = df.iloc[prev_idx]['VIX']
    ror_prev = df.iloc[prev_idx]['risk_on_ratio']

    allow_long = vix_avg < vix_prev and ror_avg > ror_prev and ema_avg > 0
    allow_short = vix_avg > vix_prev and ror_avg < ror_prev and ema_avg < 0

    if not allow_long and not allow_short:
        pb.push_note("Trade Blocked", f"No valid trade setup on {today.date()} — both long and short disallowed.")
        sys.exit("Strategy terminated: No valid trade setup. Both long and short disallowed.")

    entry_time = entry_price = exit_price = direction = None

    # Long: first breakout over high after a retest
    if len(breakout_up_times) >= 1:
        first_breakout_up = breakout_up_times[0]
        first_breakout_down = breakout_down_times[0] if len(breakout_down_times) >= 1 else None

        if not first_breakout_down or first_breakout_up < first_breakout_down:
            entry_time = first_breakout_up
            entry_price = rest_of_day.loc[entry_time, 'Close']
            direction = 'long'

    # Short: second breakout below low after a retest
    if len(breakout_down_times) >= 2:
        entry_time = breakout_down_times[1]
        entry_price = rest_of_day.loc[entry_time, 'Close']
        direction = 'short'

    if direction == 'short' and opening_strength > 0.7:
        pb.push_note("Trade Blocked", f"No valid trade setup on {today.date()} — opening strength too high.")
        sys.exit("Strategy terminated: Opening strength too high")
        
    if (direction == 'long' and not allow_long) or (direction == 'short' and not allow_short):
        pb.push_note("Trade Blocked", f"Breakout blocked by regime.")
        return None  # Exit this function early
    
    if entry_time:
        stop_loss = opening_low if direction == 'long' else opening_high
        trade_data = rest_of_day.loc[entry_time:]
        for idx, row in trade_data.iterrows():
            if direction == 'long' and row['Low'] <= stop_loss:
                exit_price = stop_loss
                break
            elif direction == 'short' and row['High'] >= stop_loss:
                exit_price = stop_loss
                break
        if not exit_price:
            exit_price = rest_of_day.iloc[-1]['Close']

        try:
            results.append({
                'date': today, 'direction': direction, 'Datetime': entry_time,
                'entry_price': entry_price, 'stop_loss': stop_loss
            })
        except Exception as e:
            print(f"PnL error on {today}: {e}")
    return pd.DataFrame(results)

def notify_trade(trade):
    if trade is not None and not trade.empty:
        last_trade = trade.iloc[-1]
        pb.push_note("New Trade Entry Detected",
                     f"{last_trade['direction'].capitalize()} at {last_trade['entry_price']} on {last_trade['Datetime']}")

# === Scheduler ===
while True:
    now_et = datetime.now(et)
    market_open, market_close = get_market_open_close()
    if market_open < now_et < market_close:
        try:
            df = fetch_data()
            trade_df = run_strategy(df)
            if trade_df:
                notify_trade(trade_df)
        except Exception as e:
            print("Runtime Error:", e)
    else:
        print("Market closed. Waiting...")
    time.sleep(300)  # 5 minutes
