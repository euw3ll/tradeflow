import logging
import asyncio
import pytz
from database.models import PendingSignal
from services.signal_parser import SignalType
from services.bybit_service import get_account_info, cancel_order, get_order_status 
from datetime import datetime, time, timedelta 
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
import os, re, subprocess
from telegram.ext import ContextTypes, ConversationHandler
from telegram.error import BadRequest, TimedOut
from database.session import SessionLocal
from database.models import User, InviteCode, MonitoredTarget, Trade, SignalForApproval
from .keyboards import (
    main_menu_keyboard, confirm_remove_keyboard, admin_menu_keyboard, 
    dashboard_menu_keyboard, settings_menu_keyboard, view_targets_keyboard, 
    bot_config_keyboard, performance_menu_keyboard, confirm_manual_close_keyboard,
    signal_filters_keyboard, ma_timeframe_keyboard, risk_menu_keyboard,
    stopgain_menu_keyboard, circuit_menu_keyboard, tp_strategy_menu_keyboard,
    invite_welcome_keyboard, invite_info_keyboard,
    onboarding_risk_keyboard, onboarding_terms_keyboard,
    settings_root_keyboard, notifications_menu_keyboard, info_menu_keyboard,
    initial_stop_menu_keyboard,
    tp_presets_keyboard,
)
from utils.security import encrypt_data, decrypt_data
from services.bybit_service import (
    get_account_info, 
    close_partial_position, 
    get_open_positions_with_pnl,
    get_market_price
)
from utils.config import ADMIN_ID
from database.crud import get_user_by_id
from core.trade_manager import _execute_trade, _execute_limit_order_for_user
from core.performance_service import generate_performance_report
from services.currency_service import get_usd_to_brl_rate
from sqlalchemy.sql import func

# Estados para as conversas
(WAITING_CODE, WAITING_API_KEY, WAITING_API_SECRET, CONFIRM_REMOVE_API) = range(4)
(ASKING_ENTRY_PERCENT, ASKING_MAX_LEVERAGE, ASKING_MIN_CONFIDENCE) = range(10, 13)
(ASKING_PROFIT_TARGET, ASKING_LOSS_LIMIT) = range(13, 15)
ASKING_STOP_GAIN_TRIGGER, ASKING_STOP_GAIN_LOCK = range(16, 18)
ASKING_CIRCUIT_THRESHOLD, ASKING_CIRCUIT_PAUSE = range(18, 20)
ASKING_COIN_WHITELIST = 15
(ASKING_MA_PERIOD, ASKING_MA_TIMEFRAME, ASKING_RSI_OVERSOLD, ASKING_RSI_OVERBOUGHT) = range(20, 24)
ASKING_TP_DISTRIBUTION = 25
ASKING_BE_TRIGGER = 26
ASKING_TS_TRIGGER = 27
ASKING_CLEANUP_MINUTES = 28
ASKING_ALERT_CLEANUP_MINUTES = 29
ASKING_PENDING_EXPIRY_MINUTES = 30
ASKING_INITIAL_SL_FIXED = 31
ASKING_RISK_PER_TRADE = 32
ASKING_PROBE_SIZE = 33

logger = logging.getLogger(__name__)

# ---- helpers (resumos no topo dos submenus) ----
def _risk_summary(user) -> str:
    try:
        return (
            f"• Entrada: {float(getattr(user,'entry_size_percent',0) or 0):.1f}%  |  "
            f"Alav.: {int(getattr(user,'max_leverage',0) or 0)}x  |  "
            f"Conf.: {float(getattr(user,'min_confidence',0) or 0):.1f}%"
        )
    except Exception:
        return "• Parâmetros indisponíveis"

def _stopgain_summary(user) -> str:
    try:
        return (
            f"• Gatilho: {float(getattr(user,'stop_gain_trigger_pct',0) or 0):.2f}%  |  "
            f"Trava: {float(getattr(user,'stop_gain_lock_pct',0) or 0):.2f}%"
        )
    except Exception:
        return "• Parâmetros indisponíveis"

def _circuit_summary(user) -> str:
    try:
        scope = (getattr(user,'circuit_breaker_scope','SIDE') or 'SIDE').upper()
        scope_label = 'Global' if scope == 'GLOBAL' else ('Símbolo' if scope == 'SYMBOL' else 'Direção')
        override = 'On' if bool(getattr(user,'reversal_override_enabled', False)) else 'Off'
        probe = float(getattr(user,'probe_size_factor', 0.5) or 0.5)
        probe_pct = int(round(probe * 100))
        return (
            f"• Limite: {int(getattr(user,'circuit_breaker_threshold',0) or 0)}  |  "
            f"Pausa: {int(getattr(user,'circuit_breaker_pause_minutes',0) or 0)} min\n"
            f"• Escopo: {scope_label}  |  Override: {override}  |  Probe: {probe_pct}%"
        )
    except Exception:
        return "• Parâmetros indisponíveis"

# --- FLUXO DE USUÁRIO (START, CADASTRO, MENUS) ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    telegram_user = update.effective_user
    user_in_db = get_user_by_id(telegram_user.id)
    if user_in_db:
        await update.message.reply_text(
            "Menu Principal:",
            reply_markup=main_menu_keyboard(telegram_id=telegram_user.id)
        )
        return ConversationHandler.END
    else:
        # Mensagem amigável de boas-vindas para quem ainda não tem convite
        text = (
            f"Olá, {telegram_user.first_name}! 👋\n\n"
            "O TradeFlow está em acesso antecipado. No momento, o uso é somente via convite.\n\n"
            "• Quer entender como funciona e como conseguir acesso?\n"
            "• Já tem um convite e quer entrar agora?"
        )
        await update.message.reply_text(text, reply_markup=invite_welcome_keyboard())
        return ConversationHandler.END

async def show_no_invite_info_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra explicação de funcionamento e acesso via convite."""
    query = update.callback_query
    await query.answer()
    text = (
        "ℹ️ Como funciona o TradeFlow\n\n"
        "• Monitoramos sinais de fontes de alta qualidade e gerenciamos entradas/saídas de forma disciplinada.\n"
        "• Você mantém o controle total: ajuste alavancagem, tamanho, filtros e metas no app.\n\n"
        "Acesso e convites\n\n"
        "• No momento, o acesso é somente com convite.\n"
        "• Para pedir acesso, fale com um membro da comunidade ou aguarde novas vagas.\n\n"
        "Se já tiver um convite, clique abaixo para ativá-lo."
    )
    await query.edit_message_text(text, reply_markup=invite_info_keyboard())

async def back_to_invite_welcome_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tg_user = update.effective_user
    text = (
        f"Olá, {tg_user.first_name}! 👋\n\n"
        "O TradeFlow está em acesso antecipado. No momento, o uso é somente via convite.\n\n"
        "• Quer entender como funciona e como conseguir acesso?\n"
        "• Já tem um convite e quer entrar agora?"
    )
    await query.edit_message_text(text, reply_markup=invite_welcome_keyboard())

async def enter_invite_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inicia a coleta do código de convite via callback, mudando para o estado WAITING_CODE."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Perfeito! Envie seu código de convite nesta conversa.")
    return WAITING_CODE

async def receive_invite_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code_text = update.message.text
    telegram_user = update.effective_user
    db = SessionLocal()
    try:
        invite_code = db.query(InviteCode).filter(InviteCode.code == code_text, InviteCode.is_used == False).first()
        if invite_code:
            new_user = User(telegram_id=telegram_user.id, first_name=telegram_user.first_name)
            db.add(new_user)
            invite_code.is_used = True
            db.commit()
            await update.message.reply_text(
                "✅ Cadastro realizado com sucesso! O próximo passo é configurar sua API.",
                reply_markup=main_menu_keyboard(telegram_id=telegram_user.id)
            )
            return ConversationHandler.END
        else:
            await update.message.reply_text("❌ Código de convite inválido ou já utilizado. Tente novamente.")
            return WAITING_CODE
    finally:
        db.close()

async def back_to_main_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Menu Principal:",
        reply_markup=main_menu_keyboard(telegram_id=update.effective_user.id)
    )

async def open_settings_root_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Abre o menu raiz de Configurações consolidado."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        text="⚙️ Configurações",
        reply_markup=settings_root_keyboard()
    )

async def notifications_settings_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Abre a seção de Configurações de Notificações."""
    query = update.callback_query
    await query.answer()
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == query.from_user.id).first()
    finally:
        db.close()

    mode = getattr(user, 'msg_cleanup_mode', 'OFF') if user else 'OFF'
    delay = int(getattr(user, 'msg_cleanup_delay_minutes', 30) or 30) if user else 30
    mode_human = 'Desativada' if mode == 'OFF' else ('Após ' + str(delay) + ' min' if mode == 'AFTER' else 'Fim do dia')

    alert_mode = getattr(user, 'alert_cleanup_mode', 'OFF') if user else 'OFF'
    alert_delay = int(getattr(user, 'alert_cleanup_delay_minutes', 30) or 30) if user else 30
    alert_human = 'Desativada' if alert_mode == 'OFF' else ('Após ' + str(alert_delay) + ' min' if alert_mode == 'AFTER' else 'Fim do dia')

    await query.edit_message_text(
        text=(
            "🔔 <b>Configurações de Notificações</b>\n\n"
            "• Fechados: <b>" + mode_human + "</b>\n"
            "• Alertas gerais: <b>" + alert_human + "</b>\n"
            "• Dica: mensagens ativas podem ser recriadas abaixo."
        ),
        parse_mode='HTML',
        reply_markup=notifications_menu_keyboard(user)
    )

async def refresh_active_messages_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Apaga (se existir) e recria as mensagens ativas de trades para este usuário."""
    query = update.callback_query
    await query.answer("Recriando mensagens...")
    user_id = update.effective_user.id
    db = SessionLocal()
    recreated = 0
    try:
        active_trades = db.query(Trade).filter(
            Trade.user_telegram_id == user_id,
            ~Trade.status.like('%CLOSED%')
        ).all()

        for t in active_trades:
            # Tenta apagar a mensagem antiga, se houver
            if t.notification_message_id:
                try:
                    await context.bot.delete_message(chat_id=user_id, message_id=t.notification_message_id)
                except BadRequest:
                    pass
                except Exception:
                    pass

            # Envia uma nova mensagem "viva" para ser atualizada pelo tracker
            base_lines = [
                f"🚀 <b>{t.symbol}</b> ({t.side})",
                f"Entrada: ${float(t.entry_price or 0):,.4f}",
                "Atualizações do trade serão exibidas aqui.",
            ]
            sent = await context.bot.send_message(chat_id=user_id, text="\n".join(base_lines), parse_mode='HTML')
            t.notification_message_id = sent.message_id
            db.commit()
            recreated += 1

        await query.edit_message_text(
            text=(f"✅ Mensagens ativas recriadas: {recreated}." if recreated > 0 else
                  "ℹ️ Não há trades ativos para recriar mensagens."),
            reply_markup=notifications_menu_keyboard()
        )
    except Exception as e:
        logger.error(f"Erro ao recriar mensagens ativas: {e}", exc_info=True)
        await query.edit_message_text("❌ Ocorreu um erro ao recriar as mensagens.", reply_markup=notifications_menu_keyboard())
    finally:
        db.close()

async def open_information_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra a seção 'Informações' com status do bot e última atualização."""
    query = update.callback_query
    await query.answer()

    async def _fetch_git_info():
        try:
            def _run():
                msg = subprocess.check_output(["git", "log", "-1", "--pretty=%B"], text=True).strip()
                date = subprocess.check_output(["git", "show", "-s", "--format=%ci", "HEAD"], text=True).strip()
                short = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
                return msg, date, short
            return await asyncio.to_thread(_run)
        except Exception:
            return None

    # Preferimos COMMIT_INFO gerado no deploy (confiável em Docker)
    full_msg, commit_date, commit_hash = "", "", ""
    try:
        with open("COMMIT_INFO", "r", encoding="utf-8") as f:
            lines = [l.rstrip("\n") for l in f.readlines()]
        if lines:
            commit_hash = lines[0] if len(lines) >= 1 else commit_hash
            commit_date = lines[1] if len(lines) >= 2 else commit_date
            full_msg = "\n".join(lines[2:]).strip() if len(lines) >= 3 else full_msg
    except Exception:
        pass

    if not any([full_msg, commit_date, commit_hash]):
        # Tenta git; se falhar, usa variáveis de ambiente e por fim a heurística de arquivos
        git_info = await _fetch_git_info()
        if git_info:
            full_msg, commit_date, commit_hash = git_info
        else:
            def first_env(*keys, default=""):
                for k in keys:
                    v = os.getenv(k)
                    if v:
                        return v
                return default

            full_msg = first_env("BUILD_MESSAGE", "GIT_COMMIT_MSG", "VERCEL_GIT_COMMIT_MESSAGE", default="")
            commit_date = first_env("BUILD_DATE", "GIT_COMMIT_DATE", "VERCEL_GIT_COMMIT_DATE", default="")
            commit_hash = first_env("BUILD_COMMIT", "GIT_COMMIT", "GIT_SHA", "GIT_COMMIT_SHA", "GITHUB_SHA", "VERCEL_GIT_COMMIT_SHA", default="")
            if not any([full_msg, commit_date, commit_hash]):
                def _latest_change():
                    latest_ts = 0
                    latest_path = None
                    for root, _, files in os.walk('.'):
                        for fname in files:
                            if fname.endswith(('.py', '.sql', '.ini', '.yml', '.yaml', '.txt', '.md')):
                                p = os.path.join(root, fname)
                                try:
                                    ts = os.path.getmtime(p)
                                    if ts > latest_ts:
                                        latest_ts, latest_path = ts, p
                                except Exception:
                                    continue
                    return latest_ts, latest_path
                try:
                    ts, pth = await asyncio.to_thread(_latest_change)
                    if ts:
                        from datetime import datetime
                        commit_date = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
                        commit_hash = 'local'
                        full_msg = f'Alteração mais recente em {pth}'
                except Exception:
                    pass
        commit_date = commit_date or "—"
        commit_hash = commit_hash or "—"

    # Monta status do bot para o usuário
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == query.from_user.id).first()
    finally:
        db.close()

    # Normaliza a data do commit para America/Sao_Paulo, se possível
    def _normalize_commit_date(s: str) -> str:
        if not s:
            return "—"
        from datetime import datetime
        import pytz
        br = pytz.timezone("America/Sao_Paulo")
        fmts = [
            "%Y-%m-%d %H:%M:%S %z",
            "%Y-%m-%d %H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%d %H:%M:%S",
        ]
        for fmt in fmts:
            try:
                dt = datetime.strptime(s, fmt)
                if not dt.tzinfo:
                    dt = pytz.utc.localize(dt)
                return dt.astimezone(br).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
        return s

    commit_date = _normalize_commit_date(commit_date)

    lines = (full_msg or "").splitlines()
    commit_subject = lines[0] if lines else "Não disponível"

    status_lines = ["<b>ℹ️ Informações</b>", ""]
    if user:
        bot_state = "Ativo" if user.is_active else "Pausado"
        sleep = " (Modo Dormir)" if (user.is_active and user.is_sleep_mode_enabled) else ""
        approval = "Manual" if str(user.approval_mode).upper() == 'MANUAL' else "Automático"
        risk = f"{float(user.entry_size_percent or 0):.1f}% @ {int(user.max_leverage or 0)}x (conf. mín. {float(user.min_confidence or 0):.0f}%)"
        stop_strategy = (getattr(user, 'stop_strategy', '') or '').upper()
        stop_strategy_label = "Breakeven" if stop_strategy.startswith('BREAKEVEN') or stop_strategy.startswith('BREAK') else "Trailing"
        stopgain = f"{stop_strategy_label} • gatilho {float(user.stop_gain_trigger_pct or 0):.2f}% / trava {float(user.stop_gain_lock_pct or 0):.2f}%"
        filters = []
        if getattr(user, 'is_ma_filter_enabled', False): filters.append("MA")
        if getattr(user, 'is_rsi_filter_enabled', False): filters.append("RSI")
        filters_text = ", ".join(filters) if filters else "Nenhum"
        whitelist = getattr(user, 'coin_whitelist', '') or 'todas'
        tp_raw = getattr(user, 'tp_distribution', 'EQUAL') or 'EQUAL'
        tp_token = tp_raw.upper()
        if tp_token == 'EQUAL':
            tp_distribution = 'Divisão Igual'
        elif tp_token == 'FRONT_HEAVY':
            tp_distribution = 'Mais cedo (frente)'
        elif tp_token == 'BACK_HEAVY':
            tp_distribution = 'Mais tarde (traseira)'
        elif tp_token == 'EXP_FRONT':
            tp_distribution = 'Exponencial cedo'
        elif ',' in (tp_raw or ''):
            tp_distribution = 'Personalizada'
        else:
            tp_distribution = tp_token
        be_trg = float(getattr(user, 'be_trigger_pct', 0) or 0)
        ts_trg = float(getattr(user, 'ts_trigger_pct', 0) or 0)
        ma_period = int(getattr(user, 'ma_period', 0) or 0)
        ma_timeframe = str(getattr(user, 'ma_timeframe', '60') or '60')
        rsi_overbought = int(getattr(user, 'rsi_overbought_threshold', 0) or 0)
        rsi_oversold = int(getattr(user, 'rsi_oversold_threshold', 0) or 0)
        daily_p = float(getattr(user, 'daily_profit_target', 0) or 0)
        daily_l = float(getattr(user, 'daily_loss_limit', 0) or 0)
        circuit_th = int(getattr(user, 'circuit_breaker_threshold', 0) or 0)
        circuit_pause = int(getattr(user, 'circuit_breaker_pause_minutes', 0) or 0)
        cb_scope = (getattr(user, 'circuit_breaker_scope', 'SIDE') or 'SIDE').upper()
        cb_scope_label = 'Global' if cb_scope == 'GLOBAL' else ('Símbolo' if cb_scope == 'SYMBOL' else 'Direção')
        cb_override = 'On' if bool(getattr(user, 'reversal_override_enabled', False)) else 'Off'
        cb_probe_pct = int(round(float(getattr(user, 'probe_size_factor', 0.5) or 0.5) * 100))
        bybit_link = "Conectado" if getattr(user, 'api_key_encrypted', None) else "Não conectado"
        # Stop Inicial
        sl_mode = (getattr(user, 'initial_sl_mode', 'ADAPTIVE') or 'ADAPTIVE').upper()
        if sl_mode == 'FIXED':
            sl_text = f"Fixo ({float(getattr(user, 'initial_sl_fixed_pct', 1.0) or 1.0):.2f}%)"
        elif sl_mode in ('FOLLOW', 'FOLLOW_SIGNAL', 'SIGNAL'):
            sl_text = "Seguir SL do Sinal"
        else:
            sl_text = f"Adaptativo (risco {float(getattr(user, 'risk_per_trade_pct', 1.0) or 1.0):.2f}% por trade)"
        # Expiração de Pendentes
        pend_exp = int(getattr(user, 'pending_expiry_minutes', 0) or 0)
        pend_text = f"{pend_exp} min" if pend_exp > 0 else "Desativado"

        # TP display: inclui âncoras quando personalizada
        tp_line = f"• TP: <b>{tp_distribution}</b>"
        if tp_distribution == 'Personalizada':
            try:
                parts = [p.strip() for p in (tp_raw or '').split(',') if p.strip()]
                fmt_list = []
                for p in parts:
                    v = float(p)
                    if abs(v - round(v)) < 1e-9:
                        fmt_list.append(str(int(round(v))))
                    else:
                        s = (f"{v:.2f}").rstrip('0').rstrip('.')
                        fmt_list.append(s)
                tp_line = f"• TP: <b>Personalizada</b> ({','.join(fmt_list)})"
            except Exception:
                pass

        status_lines += [
            f"• Bot: <b>{bot_state}{sleep}</b>",
            f"• Aprovação: <b>{approval}</b>",
            f"• Bybit: <b>{bybit_link}</b>",
            f"• Risco: <b>{risk}</b>",
            f"• Stop‑Gain: <b>{stopgain}</b>",
            f"• Gatilhos BE/TS: <b>{be_trg:.2f}% / {ts_trg:.2f}%</b>",
            tp_line,
            f"• Stop Inicial: <b>{sl_text}</b>",
            f"• Pendentes: expirar <b>{pend_text}</b>",
            f"• Metas do dia: lucro <b>${daily_p:,.2f}</b> / perda <b>${daily_l:,.2f}</b>",
            f"• Filtros: <b>{filters_text}</b> (MA {ma_period}/{ma_timeframe}, RSI {rsi_oversold}/{rsi_overbought})",
            f"• Whitelist: <code>{whitelist}</code>",
            f"• Disjuntor: limite <b>{circuit_th}</b> / pausa <b>{circuit_pause} min</b> | "
            f"Escopo: <b>{cb_scope_label}</b> | Override: <b>{cb_override}</b> | Probe: <b>{cb_probe_pct}%</b>",
        ]
    status_lines += [
        "",
        "🛠️ <b>Última atualização</b>",
        f"• Data: {commit_date}",
        f"• Commit: <code>{commit_hash}</code>",
        f"• Mensagem: {commit_subject}",
    ]

    await query.edit_message_text("\n".join(status_lines), parse_mode='HTML', reply_markup=info_menu_keyboard())

LEARN_PAGES = [
    (
        "<b>📖 Guia — Introdução</b>\n\n"
        "Bem‑vindo! Aqui você aprende o fluxo completo do TradeFlow.\n\n"
        "🔎 <b>Coleta de sinais</b>\n"
        "• Monitoramos fontes selecionadas e padronizamos mensagens em um formato único.\n"
        "• Filtramos ruídos e extraímos símbolo, lado (LONG/SHORT), SL e TPs.\n\n"
        "🧪 <b>Pré‑filtros</b>\n"
        "• Média Móvel (MA), RSI, whitelist e confiança mínima — você decide o quanto filtrar.\n"
    ),
    (
        "<b>📖 Guia — Take Profit (TP)</b>\n\n"
        "O que é: preço(s) em que parte da posição é fechada para realizar lucro.\n\n"
        "Como o bot usa TPs:\n"
        "• Se o sinal tem <b>1 TP</b>, ele pode ser enviado diretamente à corretora.\n"
        "• Se há <b>múltiplos TPs</b>, o bot gerencia <i>fechamentos parciais</i> na sequência.\n\n"
        "Distribuição de TPs:\n"
        "• Estratégia <b>EQUAL</b>: divide igualmente entre os alvos.\n"
        "• Estratégia <b>personalizada</b> (ex.: 50,30,20): usa as âncoras e ajusta cauda para somar 100%.\n"
        "• Se houver mais TPs que âncoras, a cauda decai progressivamente e é normalizada.\n"
    ),
    (
        "<b>📖 Guia — Gestão de TPs pelo bot</b>\n\n"
        "Execução prática:\n"
        "• Cada alvo atingido fecha a fração correspondente da posição.\n"
        "• O restante segue para os próximos TPs, até zerar a posição ou ser parado pelo SL/Stop‑Gain.\n\n"
        "Observações:\n"
        "• Se o tamanho remanescente ficar pequeno (abaixo do mínimo da corretora), o bot pode fechar tudo no próximo evento.\n"
        "• A distribuição é aplicada sobre o <i>tamanho de entrada</i> já ajustado por alavancagem e regras do símbolo.\n"
    ),
    (
        "<b>📖 Guia — Stop Loss (SL)</b>\n\n"
        "O que é: preço que encerra a posição para limitar perdas.\n\n"
        "Exemplos práticos:\n"
        "• LONG: entrada 1.0000, SL 0.9800 → se o preço cair até 0.9800, a posição é fechada.\n"
        "• SHORT: entrada 1.0000, SL 1.0200 → se o preço subir até 1.0200, a posição é fechada.\n\n"
        "Regras e validações:\n"
        "• O SL precisa estar do <i>lado correto</i> do preço; o bot valida contra o preço atual e o tick do instrumento.\n"
        "• Em Stop‑Gain (Breakeven/Trailing), o SL pode ser movido automaticamente.\n"
    ),
    (
        "<b>📖 Guia — Disjuntor</b>\n\n"
        "Objetivo: pausar novas operações de uma direção (LONG/SHORT) quando há perdas recorrentes.\n\n"
        "Como funciona:\n"
        "• Você define um <b>limite</b> (ex.: 2). Se houver esse número de trades <i>ativos</i> em prejuízo na mesma direção, ativa a pausa.\n"
        "• A pausa dura o período definido (<b>pausa</b> em minutos).\n"
        "• Durante a pausa, novos sinais naquela direção são ignorados. Após o tempo, as entradas voltam normalmente.\n"
    ),
    (
        "<b>📖 Guia — Aprovação Manual vs Automática</b>\n\n"
        "Automática: o bot executa o sinal assim que ele passa pelos filtros e whitelist.\n\n"
        "Manual: você recebe botões para <b>Aprovar</b> ou <b>Rejeitar</b> cada entrada.\n\n"
        "Exemplos de fluxo:\n"
        "• Modo Manual → chega o sinal → você toca em Aprovar → o bot executa e começa a gerenciar SL/TP.\n"
        "• Modo Automático → chega o sinal → o bot executa diretamente, respeitando seus filtros.\n"
        "Você pode alternar o modo em Configuração do Bot a qualquer momento.\n"
    ),
    (
        "<b>📖 Guia — Risco & Tamanho</b>\n\n"
        "🎛️ <b>Tamanho de entrada</b>\n"
        "• Percentual do seu saldo disponível usado em cada trade.\n\n"
        "⚙️ <b>Alavancagem Máxima</b>\n"
        "• Limite superior de alavancagem para controlar exposição.\n\n"
        "🎯 <b>Confiança mínima</b>\n"
        "• Bloqueia sinais abaixo do nível escolhido.\n\n"
        "📅 <b>Metas do dia</b>\n"
        "• Lucro/Perda diária para manter disciplina e evitar overtrading.\n"
    ),
    (
        "<b>📖 Guia — Execução</b>\n\n"
        "🧾 <b>Ordens</b>\n"
        "• Mercado: entra imediatamente; Limite: posiciona no preço desejado.\n"
        "• Validamos o SL contra o preço atual e as regras do instrumento (tick/step).\n\n"
        "🎯 <b>Take Profits</b>\n"
        "• Único TP pode ser enviado à corretora; múltiplos TPs são gerenciados pelo bot.\n"
    ),
    (
        "<b>📖 Guia — Stop‑Gain</b>\n\n"
        "🛡️ <b>Proteção de ganhos</b>\n"
        "• <b>Gatilho</b>: ativa a proteção a partir de certo ganho (%).\n"
        "• <b>Breakeven</b>: SL no preço de entrada para tirar risco.\n"
        "• <b>Trailing</b>: SL ‘persegue’ o preço, preservando parte do lucro.\n"
        "• <b>Trava</b>: percentual que congela parte do ganho ao acionar.\n"
    ),
    (
        "<b>📖 Guia — Fechamentos & Status</b>\n\n"
        "✅ <b>Fechamento</b>\n"
        "• Por alvo (TP), Stop, manual ou externo.\n\n"
        "🏷️ <b>Status</b>\n"
        "• ⏳ Em andamento: posição aberta (P/L ao vivo).\n"
        "• 🏆 Lucro: terminou positivo.\n"
        "• 🛑 Prejuízo/Stop: terminou no SL ou negativo.\n"
        "• ✅ Manual: você encerrou.\n"
        "• ℹ️ Externo: encerrou fora do bot.\n"
    ),
    (
        "<b>📖 Guia — Dúvidas frequentes</b>\n\n"
        "• <b>Por que o preço de entrada foi diferente do canal?</b> Slippage, latência e liquidez podem variar.\n"
        "• <b>Por que fechou antes do TP?</b> Stop‑Gain/Trailing pode ter protegido ganhos.\n"
        "• <b>O que é whitelist?</b> Lista de pares permitidos; use categorias (bluechips, altcoins).\n"
    ),
]

def _learn_nav_keyboard(idx: int) -> InlineKeyboardMarkup:
    total = len(LEARN_PAGES)
    prev_idx = (idx - 1) % total
    next_idx = (idx + 1) % total
    row = [
        InlineKeyboardButton("⬅️ Anterior", callback_data=f'info_learn_nav_prev_{idx}'),
        InlineKeyboardButton("Próxima ➡️", callback_data=f'info_learn_nav_next_{idx}')
    ]
    return InlineKeyboardMarkup([row, [InlineKeyboardButton("⬅️ Voltar", callback_data='open_info')]])

async def info_learn_start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(LEARN_PAGES[0], parse_mode='HTML', reply_markup=_learn_nav_keyboard(0))

async def info_learn_nav_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    # format: info_learn_nav_(prev|next)_<idx>
    m = re.match(r'^info_learn_nav_(prev|next)_(\d+)$', query.data or '')
    if m:
        direction = m.group(1)
        idx = int(m.group(2))
    else:
        direction, idx = 'next', 0
    total = len(LEARN_PAGES)
    if direction == 'prev':
        new_idx = (idx - 1) % total
    else:
        new_idx = (idx + 1) % total
    await query.edit_message_text(LEARN_PAGES[new_idx], parse_mode='HTML', reply_markup=_learn_nav_keyboard(new_idx))

# --- FLUXO DE CONFIGURAÇÃO DE API ---
async def config_api(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inicia o fluxo de configuração de API com um tutorial melhorado."""
    query = update.callback_query
    await query.answer()
    
    context.user_data['entry_message_id'] = query.message.message_id
    
    tutorial_text = (
        "🔑 <b>Como Criar suas Chaves de API na Bybit</b> 🔑\n\n"
        "Siga estes passos com atenção para conectar sua conta:\n\n"
        "1️⃣  Faça login em <b>Bybit.com</b> e vá para <i>Perfil > API</i>.\n\n"
        "2️⃣  Clique em <b>'Criar Nova Chave'</b> e selecione <i>'Chaves Geradas pelo Sistema'</i>.\n\n"
        "3️⃣  Dê um nome para sua chave (ex: `TradeFlowBot`) e selecione as permissões de <b>'Leitura e Escrita'</b>.\n\n"
        "4️⃣  Nas permissões, marque <b>APENAS</b> as seguintes caixas:\n"
        "   - <b>Contrato</b> (`Contract`): ✅ `Ordens` e ✅ `Posições`\n"
        "   - <b>Trading Unificado</b> (`UTA`): ✅ `Trade`\n\n"
        "5️⃣  🛡️ <b>MUITO IMPORTANTE:</b> Por segurança, <b>NÃO</b> marque a permissão de <i>'Saque' (Withdraw)</i>.\n\n"
        "⚠️ <b>Atenção:</b> Este bot opera exclusivamente com pares de trade terminados em **USDT**.\n\n"
        "6️⃣  Conclua a verificação de segurança e copie sua <b>API Key</b> e <b>API Secret</b>.\n\n"
        "-------------------------------------\n"
        "Pronto! Agora, por favor, envie sua <b>API Key</b>."
    )
    
    await query.edit_message_text(
        text=tutorial_text,
        parse_mode='HTML'
    )
    return WAITING_API_KEY

async def receive_api_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a API Key, apaga a mensagem do usuário e pede a API Secret."""
    await context.bot.delete_message(
        chat_id=update.effective_chat.id,
        message_id=update.message.message_id
    )

    api_key = update.message.text
    context.user_data['api_key'] = api_key
    
    prompt_message = await update.message.reply_text(
        "Chave API recebida com segurança. Agora, por favor, envie sua *API Secret*.",
        parse_mode='Markdown'
    )
    context.user_data['prompt_message_id'] = prompt_message.message_id
    
    return WAITING_API_SECRET

async def receive_api_secret(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a API Secret, apaga as mensagens, criptografa e salva no banco."""
    await context.bot.delete_message(
        chat_id=update.effective_chat.id,
        message_id=update.message.message_id
    )

    prompt_message_id = context.user_data.get('prompt_message_id')
    if prompt_message_id:
        await context.bot.delete_message(
            chat_id=update.effective_chat.id,
            message_id=prompt_message_id
        )

    api_secret = update.message.text
    api_key = context.user_data.get('api_key')
    telegram_id = update.effective_user.id

    encrypted_key = encrypt_data(api_key)
    encrypted_secret = encrypt_data(api_secret)

    # Valida as credenciais na Bybit antes de salvar
    try:
        account_info = await get_account_info(api_key, api_secret)
    except Exception as e:
        account_info = {"success": False, "error": str(e)}

    if not account_info.get("success"):
        # Não salva credenciais inválidas
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=context.user_data.get('entry_message_id'),
            text=(
                "❌ Não consegui validar suas credenciais da Bybit.\n"
                f"Erro: {account_info.get('error','desconhecido')}\n\n"
                "Por favor, envie novamente sua API Key."
            ),
        )
        context.user_data.pop('api_key', None)
        # Volta para pedir a API Key
        return WAITING_API_KEY

    # Sucesso: salva e inicia funil de onboarding com bot desativado por padrão
    total_equity = (account_info.get("data") or {}).get("total_equity", 0.0)
    context.user_data['onboarding_equity'] = total_equity

    db = SessionLocal()
    try:
        user_to_update = db.query(User).filter(User.telegram_id == telegram_id).first()
        if not user_to_update:
            await update.message.reply_text("Ocorreu um erro. Usuário não encontrado.")
            return ConversationHandler.END

        user_to_update.api_key_encrypted = encrypted_key
        user_to_update.api_secret_encrypted = encrypted_secret
        # Bot inicia pausado após conectar a Bybit
        user_to_update.is_active = False
        db.commit()

        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=context.user_data['entry_message_id'],
            text=(
                "✅ Suas chaves de API foram validadas e salvas!\n"
                "Por segurança, o bot inicia PAUSADO. Você pode ativá‑lo no painel."
            ),
        )
        # Inicia o funil de seleção de modo (conservador/mediano/agressivo/manual)
        await show_onboarding_risk_options(update, context, total_equity)
    finally:
        db.close()
        # Mantém onboarding_equity, limpa o resto
        context.user_data.pop('prompt_message_id', None)
        context.user_data.pop('entry_message_id', None)
        context.user_data.pop('api_key', None)

    return ConversationHandler.END

# --- Notificações: Toggle limpeza ---
async def toggle_cleanup_mode_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == query.from_user.id).first()
        if not user:
            await query.edit_message_text("Usuário não encontrado.")
            return
        cur = (getattr(user, 'msg_cleanup_mode', 'OFF') or 'OFF').upper()
        nxt = 'AFTER' if cur == 'OFF' else ('EOD' if cur == 'AFTER' else 'OFF')
        user.msg_cleanup_mode = nxt
        db.commit()
        alert_mode = (getattr(user, 'alert_cleanup_mode', 'OFF') or 'OFF').upper()
        await query.edit_message_text(
            text=(
                "🔔 <b>Configurações de Notificações</b>\n\n"
                f"• Fechados: <b>{'Desativada' if nxt=='OFF' else ('Após ' + str(int(user.msg_cleanup_delay_minutes or 30)) + ' min' if nxt=='AFTER' else 'Fim do dia')}</b>\n"
                f"• Alertas gerais: <b>{'Desativada' if alert_mode=='OFF' else ('Após ' + str(int(getattr(user,'alert_cleanup_delay_minutes',30) or 30)) + ' min' if alert_mode=='AFTER' else 'Fim do dia')}</b>\n"
            ),
            parse_mode='HTML',
            reply_markup=notifications_menu_keyboard(user)
        )
    finally:
        db.close()

async def ask_cleanup_minutes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['settings_message_id'] = query.message.message_id
    await query.edit_message_text(
        text=(
            "Digite o tempo (em minutos) para excluir mensagens de trades já fechados.\n"
            "Ex.: 30. Use 0 para desativar (equivale a OFF)."
        )
    )
    return ASKING_CLEANUP_MINUTES

async def receive_cleanup_minutes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    text = (update.message.text or '').strip().replace(',', '.')
    message_id_to_edit = context.user_data.get('settings_message_id')
    try:
        n = int(float(text))
        if n < 0:
            raise ValueError
    except Exception:
        try: await update.message.delete()
        except Exception: pass
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit,
                                            text="Valor inválido. Envie um número inteiro (ex.: 30).")
        return ASKING_CLEANUP_MINUTES

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == user_id).first()
        if not user:
            await update.message.reply_text("Usuário não encontrado.")
            return ConversationHandler.END
        if n == 0:
            user.msg_cleanup_mode = 'OFF'
        else:
            user.msg_cleanup_mode = 'AFTER'
            user.msg_cleanup_delay_minutes = n
        db.commit()
    finally:
        db.close()

    # Mostra menu de notificações atualizado
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == user_id).first()
    finally:
        db.close()
    try: await update.message.delete()
    except Exception: pass
    await context.bot.edit_message_text(
        chat_id=user_id,
        message_id=message_id_to_edit,
        text=(
            "🔔 <b>Configurações de Notificações</b>\n\n"
            f"• Fechados: <b>{'Desativada' if user.msg_cleanup_mode=='OFF' else ('Após ' + str(int(user.msg_cleanup_delay_minutes or 30)) + ' min' if user.msg_cleanup_mode=='AFTER' else 'Fim do dia')}</b>\n"
            f"• Alertas gerais: <b>{'Desativada' if getattr(user,'alert_cleanup_mode','OFF')=='OFF' else ('Após ' + str(int(getattr(user,'alert_cleanup_delay_minutes',30) or 30)) + ' min' if getattr(user,'alert_cleanup_mode','OFF')=='AFTER' else 'Fim do dia')}</b>\n"
        ),
        parse_mode='HTML',
        reply_markup=notifications_menu_keyboard(user)
    )
    return ConversationHandler.END

# --- Notificações: Toggle limpeza de ALERTAS ---
async def toggle_alert_cleanup_mode_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == query.from_user.id).first()
        if not user:
            await query.edit_message_text("Usuário não encontrado.")
            return
        cur = (getattr(user, 'alert_cleanup_mode', 'OFF') or 'OFF').upper()
        nxt = 'AFTER' if cur == 'OFF' else ('EOD' if cur == 'AFTER' else 'OFF')
        user.alert_cleanup_mode = nxt
        db.commit()
        await query.edit_message_text(
            text=(
                "🔔 <b>Configurações de Notificações</b>\n\n"
                f"• Fechados: <b>{'Desativada' if getattr(user,'msg_cleanup_mode','OFF')=='OFF' else ('Após ' + str(int(getattr(user,'msg_cleanup_delay_minutes',30) or 30)) + ' min' if getattr(user,'msg_cleanup_mode','OFF')=='AFTER' else 'Fim do dia')}</b>\n"
                f"• Alertas gerais: <b>{'Desativada' if nxt=='OFF' else ('Após ' + str(int(getattr(user,'alert_cleanup_delay_minutes',30) or 30)) + ' min' if nxt=='AFTER' else 'Fim do dia')}</b>\n"
            ),
            parse_mode='HTML',
            reply_markup=notifications_menu_keyboard(user)
        )
    finally:
        db.close()

async def ask_alert_cleanup_minutes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['settings_message_id'] = query.message.message_id
    await query.edit_message_text(
        text=(
            "Digite o tempo (em minutos) para excluir mensagens de ALERTA (erros/avisos).\n"
            "Ex.: 30. Use 0 para desativar (equivale a OFF)."
        )
    )
    return ASKING_ALERT_CLEANUP_MINUTES

async def receive_alert_cleanup_minutes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    text = (update.message.text or '').strip().replace(',', '.')
    message_id_to_edit = context.user_data.get('settings_message_id')
    try:
        n = int(float(text))
        if n < 0:
            raise ValueError
    except Exception:
        try: await update.message.delete()
        except Exception: pass
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit,
                                            text="Valor inválido. Envie um número inteiro (ex.: 30).")
        return ASKING_ALERT_CLEANUP_MINUTES

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == user_id).first()
        if not user:
            await update.message.reply_text("Usuário não encontrado.")
            return ConversationHandler.END
        if n == 0:
            user.alert_cleanup_mode = 'OFF'
        else:
            user.alert_cleanup_mode = 'AFTER'
            user.alert_cleanup_delay_minutes = n
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == user_id).first()
    finally:
        db.close()
    try: await update.message.delete()
    except Exception: pass
    await context.bot.edit_message_text(
        chat_id=user_id,
        message_id=message_id_to_edit,
        text=(
            "🔔 <b>Configurações de Notificações</b>\n\n"
            f"• Fechados: <b>{'Desativada' if getattr(user,'msg_cleanup_mode','OFF')=='OFF' else ('Após ' + str(int(getattr(user,'msg_cleanup_delay_minutes',30) or 30)) + ' min' if getattr(user,'msg_cleanup_mode','OFF')=='AFTER' else 'Fim do dia')}</b>\n"
            f"• Alertas gerais: <b>{'Desativada' if user.alert_cleanup_mode=='OFF' else ('Após ' + str(int(user.alert_cleanup_delay_minutes or 30)) + ' min' if user.alert_cleanup_mode=='AFTER' else 'Fim do dia')}</b>\n"
        ),
        parse_mode='HTML',
        reply_markup=notifications_menu_keyboard(user)
    )
    return ConversationHandler.END

def _format_currency(v: float) -> str:
    try:
        return f"${v:,.2f}"
    except Exception:
        return f"${v}"

def _compute_recommendations(equity: float) -> dict:
    """Gera recomendações para cada modo com base no patrimônio atual."""
    eq = float(equity or 0.0)
    def rec(entry_pct, lev, conf, stop_trg, stop_lock, loss_pct, profit_pct):
        return {
            "entry_size_percent": entry_pct,
            "max_leverage": lev,
            "min_confidence": conf,
            "stop_gain_trigger_pct": stop_trg,
            "stop_gain_lock_pct": stop_lock,
            "daily_loss_limit": round(eq * (loss_pct/100.0), 2),
            "daily_profit_target": round(eq * (profit_pct/100.0), 2),
        }
    return {
        "conservative": rec(2.0, 5, 75.0, 1.5, 0.5, 2.0, 1.0),
        "moderate":    rec(5.0, 10, 65.0, 2.0, 0.7, 3.0, 1.5),
        "aggressive":   rec(10.0, 20, 55.0, 3.0, 1.0, 5.0, 2.0),
    }

async def show_onboarding_risk_options(update: Update, context: ContextTypes.DEFAULT_TYPE, equity: float):
    chat_id = update.effective_user.id
    recs = _compute_recommendations(equity)
    # Lê algumas configs atuais do usuário para exibir
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == chat_id).first()
    finally:
        db.close()
    if user is not None:
        sl_mode = (getattr(user, 'initial_sl_mode', 'ADAPTIVE') or 'ADAPTIVE').upper()
        if sl_mode == 'FIXED':
            sl_text = f"Fixo ({float(getattr(user, 'initial_sl_fixed_pct', 1.0) or 1.0):.2f}%)"
        elif sl_mode in ('FOLLOW', 'FOLLOW_SIGNAL', 'SIGNAL'):
            sl_text = "Seguir SL do Sinal"
        else:
            sl_text = f"Adaptativo (risco {float(getattr(user, 'risk_per_trade_pct', 1.0) or 1.0):.2f}% por trade)"
        pend_exp = int(getattr(user, 'pending_expiry_minutes', 0) or 0)
        pend_text = f"{pend_exp} min" if pend_exp > 0 else "Desativado"
    else:
        sl_text = "Adaptativo (risco 1.00% por trade)"
        pend_text = "Desativado"
    def line(name, r):
        approx_entry = equity * (r["entry_size_percent"]/100.0)
        return (
            f"<b>{name}</b>\n"
            f"• Tamanho: {r['entry_size_percent']:.1f}% (~{_format_currency(approx_entry)})\n"
            f"• Alavancagem máx.: {r['max_leverage']}x\n"
            f"• Confiança mínima: {r['min_confidence']:.0f}%\n"
            f"• Stop‑Gain: gatilho {r['stop_gain_trigger_pct']:.2f}% | trava {r['stop_gain_lock_pct']:.2f}%\n"
            f"• Limites diários: lucro {_format_currency(r['daily_profit_target'])} | perda {_format_currency(r['daily_loss_limit'])}\n"
        )
    msg = (
        "🎯 Escolha como quer começar\n\n"
        f"Saldo detectado: <b>{_format_currency(equity)}</b>\n\n"
        f"{line('🟢 Conservador', recs['conservative'])}\n"
        f"{line('🟠 Mediano', recs['moderate'])}\n"
        f"{line('🔴 Agressivo', recs['aggressive'])}\n"
        f"• Stop Inicial: <b>{sl_text}</b>\n"
        f"• Pendentes: expirar <b>{pend_text}</b>\n\n"
        "Você pode alterar tudo depois em Configurações.\n\n"
        "Ou escolha <b>Configuração Manual</b> para ajustar do zero."
    )
    await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode='HTML', reply_markup=onboarding_risk_keyboard())

async def onboard_select_preset_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Aplica o preset escolhido (ou manual) e mostra os termos."""
    query = update.callback_query
    await query.answer()

    choice = query.data.replace('onboard_risk_', '')
    equity = float(context.user_data.get('onboarding_equity') or 0.0)

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == update.effective_user.id).first()
        if not user:
            await query.edit_message_text("Usuário não encontrado. Use /start para registrar.")
            return

        if choice == 'manual':
            # Zera para o usuário configurar depois; bot permanece pausado
            user.entry_size_percent = 0.0
            user.max_leverage = 0
            user.min_confidence = 0.0
            # Mantém stop-gain e metas padrão (0 = desativado)
        else:
            recs = _compute_recommendations(equity).get(choice)
            if recs:
                user.entry_size_percent = recs['entry_size_percent']
                user.max_leverage = recs['max_leverage']
                user.min_confidence = recs['min_confidence']
                user.stop_gain_trigger_pct = recs['stop_gain_trigger_pct']
                user.stop_gain_lock_pct = recs['stop_gain_lock_pct']
                user.daily_loss_limit = recs['daily_loss_limit']
                user.daily_profit_target = recs['daily_profit_target']

        db.commit()

        # Exibe termos de responsabilidade
        terms = (
            "📜 Termo de Responsabilidade\n\n"
            "• Este bot NÃO promete ganhos e NÃO garante resultados.\n"
            "• Operar mercados envolve riscos significativos, incluindo perdas parciais ou totais do capital.\n"
            "• Você é o único responsável por suas operações e configurações.\n"
            "• Monitoramos fontes de alta qualidade, mas risco sempre existe.\n\n"
            "Para concluir a ativação do app, confirme que você leu e concorda."
        )
        await query.edit_message_text(terms, reply_markup=onboarding_terms_keyboard())
    finally:
        db.close()

async def onboard_accept_terms_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    await query.edit_message_text("✅ Obrigado! Configuração inicial concluída.")
    await context.bot.send_message(
        chat_id=user_id,
        text="Menu Principal:",
        reply_markup=main_menu_keyboard(telegram_id=user_id)
    )

async def onboard_decline_terms_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Você precisa aceitar o termo para concluir a configuração. Você pode voltar ao /start quando quiser.")

# --- FLUXO DE REMOÇÃO DE API ---
async def remove_api_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        text="⚠️ Você tem certeza que deseja remover suas chaves de API?",
        reply_markup=confirm_remove_keyboard()
    )
    return CONFIRM_REMOVE_API

async def remove_api_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id

    if query.data == 'remove_api_confirm':
        db = SessionLocal()
        try:
            user_to_update = db.query(User).filter(User.telegram_id == telegram_id).first()
            if user_to_update:
                user_to_update.api_key_encrypted = None
                user_to_update.api_secret_encrypted = None
                db.commit()
            await query.edit_message_text("✅ Suas chaves de API foram removidas.")
        finally:
            db.close()
    else: # Cancelou
        await query.edit_message_text("Operação cancelada.")

    await context.bot.send_message(
        chat_id=telegram_id,
        text="Menu Principal:",
        reply_markup=main_menu_keyboard(telegram_id=telegram_id)
    )
    return ConversationHandler.END

async def my_positions_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except BadRequest as e:
        logger.warning(f"Não foi possível responder ao callback_query (pode ser antigo): {e}")
        return

    try:
        await query.edit_message_text("Buscando suas posições gerenciadas...")
    except BadRequest as e:
        logger.warning(f"Falha ao editar mensagem para 'Buscando suas posições...': {e}")
        return

    user_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(telegram_id=user_id).first()
        if not user or not user.api_key_encrypted:
            await query.edit_message_text("Você ainda não configurou suas chaves de API.")
            return

        api_key = decrypt_data(user.api_key_encrypted)
        api_secret = decrypt_data(user.api_secret_encrypted)

        active_trades = db.query(Trade).filter(
            Trade.user_telegram_id == user_id,
            ~Trade.status.like('%CLOSED%')
        ).order_by(Trade.created_at.desc()).all()

        if not active_trades:
            # Mesmo sem posições ativas, exibe as abas para permitir navegar até Pendentes
            lines = [
                "<b>📊 Suas Posições Ativas (Gerenciadas pelo Bot)</b>",
                "",
                "Nenhuma posição sendo gerenciada."
            ]
            keyboard_rows = [[
                InlineKeyboardButton("Ativas", callback_data='user_positions'),
                InlineKeyboardButton("Pendentes", callback_data='user_pending_positions')
            ]]
            keyboard_rows.append([InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data='back_to_main_menu')])
            await query.edit_message_text(
                "\n".join(lines),
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(keyboard_rows)
            )
            return

        # DEDUPE DE EXIBIÇÃO: em caso de resíduos antigos duplicados, mostra apenas 1 trade por símbolo
        grouped = {}
        for t in active_trades:
            grouped.setdefault(t.symbol, []).append(t)
        canonical_trades = []
        for sym, items in grouped.items():
            if len(items) == 1:
                canonical_trades.append(items[0])
            else:
                # prioriza quem tem message_id e o mais recente
                items_sorted = sorted(
                    items,
                    key=lambda x: (
                        1 if getattr(x, 'notification_message_id', None) else 0,
                        getattr(x, 'created_at', None) or datetime(1970,1,1),
                        float(getattr(x, 'remaining_qty', None) or getattr(x, 'qty', 0.0) or 0.0)
                    ),
                    reverse=True
                )
                canonical_trades.append(items_sorted[0])

        live_pnl_data = {}
        live_positions_result = await get_open_positions_with_pnl(api_key, api_secret)
        if live_positions_result.get("success"):
            for pos in live_positions_result.get("data", []):
                live_pnl_data[pos["symbol"]] = pos

        lines = ["<b>📊 Suas Posições Ativas (Gerenciadas pelo Bot)</b>", ""]
        # Abas de navegação: Ativas | Pendentes
        keyboard_rows = [[
            InlineKeyboardButton("Ativas", callback_data='user_positions'),
            InlineKeyboardButton("Pendentes", callback_data='user_pending_positions')
        ]]
        
        if not canonical_trades:
             lines.append("Nenhuma posição encontrada na Bybit.")
        else:
            # COMENTÁRIO: A lógica agora itera por trade individual, sem agregação.
            for trade in canonical_trades:
                arrow = "⬆️" if trade.side == "LONG" else "⬇️"
                entry = float(trade.entry_price or 0.0)
                qty = float(trade.remaining_qty if trade.remaining_qty is not None else trade.qty)
                
                pnl_info = "  P/L: <i>buscando...</i>\n"
                pos_data = live_pnl_data.get(trade.symbol)
                if pos_data:
                    pnl_val = float(pos_data.get("unrealized_pnl", 0.0))
                    pnl_frac = float(pos_data.get("unrealized_pnl_frac", 0.0)) * 100.0
                    pnl_info = f"  P/L: <b>{pnl_val:+.2f} USDT ({pnl_frac:+.2f}%)</b>\n"
                
                # COMENTÁRIO: Nova lógica para exibir o progresso dos TPs.
                total_tps = int(trade.total_initial_targets or 0)
                remaining_tps = len(trade.initial_targets or [])
                hit_tps = total_tps - remaining_tps
                
                targets_info = ""
                if total_tps > 0:
                    targets_info = f"  🎯 TPs: <b>{hit_tps}/{total_tps} atingidos</b>\n"

                lines.append(
                    f"- {arrow} <b>{trade.symbol}</b> ({trade.side})\n"
                    f"  Qtd: {qty:g} | Entrada: ${entry:,.4f}\n"
                    f"{pnl_info}{targets_info}"
                )
                
                # Adiciona um botão de fechar para cada trade individual
                keyboard_rows.append([
                    InlineKeyboardButton(
                        f"Fechar {trade.symbol} #{trade.id} ❌",
                        callback_data=f"confirm_close_{trade.id}" # Aponta para o ID único do trade
                    )
                ])

        lines.append("<i>P/L é atualizado em tempo real pela corretora.</i>")
        keyboard_rows.append([InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data='back_to_main_menu')])
        
        try:
            await query.edit_message_text(
                "\n".join(lines), 
                parse_mode='HTML', 
                reply_markup=InlineKeyboardMarkup(keyboard_rows)
            )
        except BadRequest as e:
            # Se não for possível editar (ex.: apagada/antiga), envia nova
            logger.warning(f"Falha ao editar lista de posições: {e}. Enviando nova mensagem.")
            await context.bot.send_message(
                chat_id=user_id,
                text="\n".join(lines),
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(keyboard_rows)
            )

    finally:
        db.close()

async def pending_positions_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lista ordens limite pendentes do usuário com opção de cancelamento e sincronização com a corretora."""
    query = update.callback_query
    try:
        await query.answer()
    except BadRequest:
        return

    try:
        await query.edit_message_text("Buscando suas ordens pendentes...")
    except BadRequest:
        pass

    user_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(telegram_id=user_id).first()
        if not user or not user.api_key_encrypted:
            await query.edit_message_text("Você ainda não configurou suas chaves de API.")
            return

        api_key = decrypt_data(user.api_key_encrypted)
        api_secret = decrypt_data(user.api_secret_encrypted)

        pendentes = db.query(PendingSignal).filter_by(user_telegram_id=user_id).order_by(PendingSignal.id.desc()).all()

        lines = ["<b>⏳ Suas Ordens Pendentes</b>", ""]
        # Abas de navegação: Ativas | Pendentes
        keyboard_rows = [[
            InlineKeyboardButton("Ativas", callback_data='user_positions'),
            InlineKeyboardButton("Pendentes", callback_data='user_pending_positions')
        ]]

        cleaned_any = False
        for p in list(pendentes):
            # Sincroniza rapidamente o status antes de exibir
            status_str = "Pendente"
            created_str = ""
            try:
                st = await get_order_status(api_key, api_secret, p.order_id, p.symbol)
                if st.get("success"):
                    od = st.get("data") or {}
                    status_raw = (od.get("orderStatus") or "").strip()
                    status_str = status_raw or status_str
                    # Remove da lista local se estiver cancelada/expirada/rejeitada ou totalmente executada
                    status_upper = status_raw.upper() if status_raw else ""
                    if status_upper in ("CANCELLED", "CANCELED", "REJECTED", "EXPIRED", "DEACTIVATED"):
                        db.delete(p)
                        cleaned_any = True
                        await send_user_alert(context.application, user_id,
                                              f"ℹ️ Sua ordem limite para <b>{p.symbol}</b> não está mais aberta (status: {status_raw}). Removida da lista de pendentes.")
                        continue
                    if status_upper == "FILLED":
                        # Será promovida pelo tracker; não exibir aqui
                        db.delete(p)
                        cleaned_any = True
                        continue

                    # Tenta ler timestamp de criação
                    ts_ms = od.get("createdTime") or od.get("createTime") or od.get("createdAt") or od.get("createdAtTs")
                    try:
                        if ts_ms is not None:
                            ts_ms_f = float(ts_ms)
                            import datetime
                            dt = datetime.datetime.fromtimestamp(ts_ms_f / 1000.0)
                            created_str = dt.strftime("%d/%m %H:%M")
                    except Exception:
                        created_str = ""
            except Exception:
                pass

            side = (p.signal_data or {}).get('order_type') or '—'
            limit_price = (p.signal_data or {}).get('limit_price')
            price_txt = f"${float(limit_price):,.4f}" if isinstance(limit_price, (int, float)) else str(limit_price or '—')

            # Monta as linhas
            lines.append(
                f"- <b>{p.symbol}</b> ({side})\n"
                f"  Preço: {price_txt} | Status: <b>{status_str}</b>\n"
                f"  ID Corretora: <code>{p.order_id}</code>"
                + (f"\n  Criada: {created_str}" if created_str else "")
            )

            keyboard_rows.append([
                InlineKeyboardButton(f"Cancelar {p.symbol} #{p.id} ❌", callback_data=f"confirm_cancel_pending_{p.id}")
            ])

        if not pendentes or cleaned_any:
            # Recarrega se houve limpeza de itens para refletir o estado final
            pendentes = db.query(PendingSignal).filter_by(user_telegram_id=user_id).order_by(PendingSignal.id.desc()).all()
            if not pendentes:
                lines.append("Nenhuma ordem limite pendente.")

        keyboard_rows.append([InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data='back_to_main_menu')])

        try:
            await query.edit_message_text(
                "\n".join(lines), parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard_rows)
            )
        except BadRequest as e:
            logger.warning(f"Falha ao editar lista de pendentes: {e}. Enviando nova mensagem.")
            await context.bot.send_message(
                chat_id=user_id,
                text="\n".join(lines),
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(keyboard_rows)
            )
    finally:
        db.close()

async def cancel_pending_prompt_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirma cancelamento de uma ordem limite pendente."""
    query = update.callback_query
    await query.answer()
    ps_id_str = query.data.split('_')[-1]
    if not ps_id_str.isdigit():
        await query.edit_message_text("Este botão é de uma versão antiga. Volte e abra o menu novamente.")
        return
    ps_id = int(ps_id_str)

    db = SessionLocal()
    try:
        p = db.query(PendingSignal).filter_by(id=ps_id, user_telegram_id=update.effective_user.id).first()
        if not p:
            await query.edit_message_text("Erro: ordem pendente não encontrada.")
            return
        msg = (
            f"⚠️ <b>Confirmar Cancelamento</b> ⚠️\n\n"
            f"Cancelar sua ordem limite para <b>{p.symbol}</b>?\n"
            f"<i>ID:</i> <code>{p.order_id}</code>"
        )
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Sim, cancelar", callback_data=f"execute_cancel_pending_{p.id}"),
                InlineKeyboardButton("❌ Não, voltar", callback_data='user_pending_positions'),
            ]
        ])
        await query.edit_message_text(msg, parse_mode='HTML', reply_markup=kb)
    finally:
        db.close()

async def execute_cancel_pending_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Executa o cancelamento na corretora e remove localmente."""
    query = update.callback_query
    await query.answer("Cancelando...")
    ps_id = int(query.data.split('_')[-1])
    user_id = update.effective_user.id

    db = SessionLocal()
    try:
        p = db.query(PendingSignal).filter_by(id=ps_id, user_telegram_id=user_id).first()
        if not p:
            await query.edit_message_text("Ordem já não está mais pendente.")
            return

        user = db.query(User).filter_by(telegram_id=user_id).first()
        api_key = decrypt_data(user.api_key_encrypted)
        api_secret = decrypt_data(user.api_secret_encrypted)

        resp = await cancel_order(api_key, api_secret, p.order_id, p.symbol)
        if not resp.get("success"):
            # Trata erros idempotentes como sucesso
            err = (resp.get("error") or "").lower()
            if any(s in err for s in ("already", "not found", "canceled", "cancelled")):
                pass
            else:
                await query.edit_message_text(f"❌ Falha ao cancelar: {resp.get('error', 'erro desconhecido')}\nTente novamente.")
                return

        db.delete(p)
        db.commit()

        await send_user_alert(context.application, user_id,
                              f"✅ Sua ordem limite para <b>{p.symbol}</b> foi cancelada.")

        # Volta para a lista de pendentes atualizada
        await pending_positions_handler(update, context)
    finally:
        db.close()

async def user_dashboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exibe o painel financeiro com um resumo visual e completo dos saldos da carteira."""
    query = update.callback_query
    try:
        await query.answer()
    except BadRequest as e:
        logger.warning(f"Não foi possível responder ao callback_query (pode ser antigo): {e}")
        return

    await query.edit_message_text("Buscando informações do painel...")
    user_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = get_user_by_id(user_id)
        if not user or not user.api_key_encrypted:
            await query.edit_message_text("Você precisa configurar sua API primeiro.", reply_markup=main_menu_keyboard(telegram_id=user_id))
            return

        api_key = decrypt_data(user.api_key_encrypted)
        api_secret = decrypt_data(user.api_secret_encrypted)

        # Busca o saldo e a cotação em paralelo para mais eficiência
        account_info_task = get_account_info(api_key, api_secret)
        brl_rate_task = get_usd_to_brl_rate()
        account_info, brl_rate = await asyncio.gather(account_info_task, brl_rate_task)

        message = "💼 <b>Meu Painel Financeiro</b>\n\n"
        
        if account_info.get("success"):
            balance_data = account_info.get("data", {})
            total_equity = balance_data.get("total_equity", 0.0)

            brl_text = ""
            if brl_rate:
                total_brl = total_equity * brl_rate
                brl_text = f" (aprox. R$ {total_brl:,.2f})"

            message += f"💰 <b>Patrimônio Total:</b> ${total_equity:,.2f} USDT{brl_text}\n"
            message += "<i>(Valor total da conta, incluindo P/L de posições abertas e o valor de todas as moedas)</i>\n\n"
            message += "<b>Saldos em Carteira:</b>\n"

            coin_list = balance_data.get("coin_list", [])
            wallet_lines = []
            
            if coin_list:
                for c in coin_list:
                    coin = (c.get("coin") or "").upper()
                    wallet_balance_str = c.get("walletBalance")
                    wallet_balance = float(wallet_balance_str) if wallet_balance_str else 0.0

                    if wallet_balance > 0.00001:
                        if coin == "USDT":
                            wallet_lines.insert(0, f"  - <b>{coin}: {wallet_balance:,.2f}</b>") # Garante que USDT apareça primeiro
                        else:
                            wallet_lines.append(f"  - {coin}: {wallet_balance:g}")
            
            if wallet_lines:
                message += "\n".join(wallet_lines)
            else:
                message += "Nenhum saldo encontrado.\n"
        else:
            message += f"❌ Erro ao buscar saldo: {account_info.get('error')}\n"

        message += "\n\n⚠️ <i>Este bot opera exclusivamente com pares USDT.</i>"

        await query.edit_message_text(message, parse_mode="HTML", reply_markup=dashboard_menu_keyboard(user))

    except Exception as e:
        logger.error(f"Erro ao montar o painel do usuário: {e}", exc_info=True)
        await query.edit_message_text("Ocorreu um erro ao buscar os dados do seu painel.")
    finally:
        db.close()

# --- CANCELAMENTO ---
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancela a operação atual."""
    await update.message.reply_text("Operação cancelada.")
    return ConversationHandler.END

# --- FLUXO DE ADMINISTRAÇÃO ---
async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra o menu de administrador, se o usuário for o admin."""
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("Você não tem permissão para usar este comando.")
        return

    await update.message.reply_text(
        "Bem-vindo ao painel de administração.",
        reply_markup=admin_menu_keyboard()
    )


async def admin_view_targets_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Busca e exibe a lista de todos os canais e tópicos sendo monitorados."""
    query = update.callback_query
    await query.answer()
    
    db = SessionLocal()
    try:
        targets = db.query(MonitoredTarget).all()
        
        message = "<b>👁️ Alvos Atualmente Monitorados</b>\n\n"
        
        if targets:
            for target in targets:
                if target.topic_name:
                    message += f"- <b>Grupo:</b> {target.channel_name}\n  - <b>Tópico:</b> {target.topic_name}\n"
                else:
                    message += f"- <b>Canal:</b> {target.channel_name}\n"
        else:
            message += "Nenhum alvo sendo monitorado no momento."
            
        await query.edit_message_text(
            text=message,
            parse_mode='HTML',
            reply_markup=view_targets_keyboard()
        )
    finally:
        db.close()

async def back_to_admin_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Retorna o usuário para o menu de administração principal."""
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "Bem-vindo ao painel de administração.",
        reply_markup=admin_menu_keyboard()
    )

async def list_channels_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Coloca um pedido na fila para listar os grupos e canais do usuário."""
    query = update.callback_query
    await query.answer()
    
    comm_queue = context.application.bot_data.get('comm_queue')
    if not comm_queue:
        await query.edit_message_text("Erro: Fila de comunicação não encontrada.")
        return
    
    request_data = {
        "action": "list_channels",
        "chat_id": query.message.chat_id,
        "message_id": query.message.message_id,
    }
    
    await comm_queue.put(request_data)
    
    await query.edit_message_text("Buscando sua lista de canais... Se você tiver muitos grupos, isso pode levar até um minuto. Por favor, aguarde.")
    
async def select_channel_to_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Coloca um pedido na fila para listar tópicos (ou gerenciar um canal plano)."""
    query = update.callback_query
    await query.answer()
    comm_queue = context.application.bot_data.get('comm_queue')
    if not comm_queue: return

    user_id = update.effective_user.id
    if user_id != ADMIN_ID: return

    channel_id = int(query.data.split('_')[-1])
    
    channel_name = ""
    for row in query.message.reply_markup.inline_keyboard:
        for button in row:
            if button.callback_data == query.data:
                channel_name = button.text.replace(" ✅", "")
                break

    request_data = {
        "action": "list_topics",
        "chat_id": query.message.chat_id,
        "message_id": query.message.message_id,
        "channel_id": channel_id,
        "channel_name": channel_name
    }
    
    await comm_queue.put(request_data)
    await query.edit_message_text("Processando...")

async def select_topic_to_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Salva/remove o tópico e pede para a fila recarregar o menu de tópicos."""
    query = update.callback_query
    await query.answer() 

    comm_queue = context.application.bot_data.get('comm_queue')
    if not comm_queue:
        logger.error("Fila de comunicação não encontrada no contexto do bot.")
        return

    user_id = update.effective_user.id
    if user_id != ADMIN_ID: return

    _, _, channel_id_str, topic_id_str = query.data.split('_')
    channel_id = int(channel_id_str)
    topic_id = int(topic_id_str)
    
    db = SessionLocal()
    try:
        existing_target = db.query(MonitoredTarget).filter_by(channel_id=channel_id, topic_id=topic_id).first()
        
        if existing_target:
            db.delete(existing_target)
        else:
            topic_name = ""
            for row in query.message.reply_markup.inline_keyboard:
                for button in row:
                    if button.callback_data == query.data:
                        topic_name = button.text.replace(" ✅", "")
                        break
            new_target = MonitoredTarget(channel_id=channel_id, topic_id=topic_id, topic_name=topic_name)
            db.add(new_target)
        
        db.commit()
    finally:
        db.close()

    request_data = {
        "action": "list_topics",
        "chat_id": query.message.chat_id,
        "message_id": query.message.message_id,
        "channel_id": channel_id,
        "channel_name": ""
    }
    await comm_queue.put(request_data)

async def back_to_channels_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Retorna o usuário para a lista de canais/grupos."""
    await list_channels_handler(update, context)

# --- FUNÇÕES DUPLICADAS REMOVIDAS PARA LIMPEZA ---
# my_dashboard_handler, my_positions_handler, back_to_main_menu_handler
# já estavam definidas acima.

async def user_settings_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Abre o menu raiz de Configurações (texto padronizado + teclado novo)."""
    query = update.callback_query
    await query.answer()
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == query.from_user.id).first()
        if not user:
            await query.edit_message_text("Não encontrei seu usuário. Use /start para registrar.")
            return

        header = (
            "⚙️ <b>Configurações de Trade</b>\n"
            "<i>Escolha uma categoria para ajustar seus parâmetros.</i>"
        )
        await query.edit_message_text(
            text=header,
            reply_markup=settings_menu_keyboard(user),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"[settings] Erro ao abrir menu raiz de Configurações: {e}", exc_info=True)
        await query.edit_message_text("Não foi possível abrir as Configurações agora.")
    finally:
        db.close()

# ---- RISCO & TAMANHO ----
async def receive_entry_percent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_id_to_edit = context.user_data.get('settings_message_id')
    text = (update.message.text or "").strip().replace("%", "").replace(",", ".")
    db = SessionLocal()
    try:
        value = float(text)
        if value <= 0 or value > 100:
            try: await update.message.delete()
            except Exception: pass
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=message_id_to_edit,
                text="❌ Valor inválido. Envie um número entre 0 e 100 (ex.: 3.5)."
            )
            return ASKING_ENTRY_PERCENT
        user = db.query(User).filter_by(telegram_id=update.effective_user.id).first()
        if not user:
            await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="Usuário não encontrado. Use /start para registrar.")
            return ConversationHandler.END
        user.entry_size_percent = value; db.commit()
        try: await update.message.delete()
        except Exception: pass
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=message_id_to_edit,
            text=f"🧮 <b>Risco & Tamanho</b>\n✅ Tamanho de entrada salvo: <b>{value:.1f}%</b>",
            reply_markup=risk_menu_keyboard(user), parse_mode="HTML",
        )
    except ValueError:
        try: await update.message.delete()
        except Exception: pass
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="❌ Não entendi. Envie um número (ex.: 3.5).")
        return ASKING_ENTRY_PERCENT
    except Exception as e:
        db.rollback(); logger.error(f"[settings] entry_size_percent: {e}", exc_info=True)
        try: await update.message.delete()
        except Exception: pass
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="Erro ao salvar. Tente novamente.")
        return ASKING_ENTRY_PERCENT
    finally:
        db.close()
    return ConversationHandler.END


async def receive_max_leverage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_id_to_edit = context.user_data.get('settings_message_id')
    text = (update.message.text or "").strip().lower().replace("x", "")
    db = SessionLocal()
    try:
        value = int(float(text))
        if value < 1 or value > 125:
            try: await update.message.delete()
            except Exception: pass
            await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="❌ Valor inválido. Envie um inteiro entre 1 e 125 (ex.: 10).")
            return ASKING_MAX_LEVERAGE
        user = db.query(User).filter_by(telegram_id=update.effective_user.id).first()
        if not user:
            await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="Usuário não encontrado. Use /start para registrar.")
            return ConversationHandler.END
        user.max_leverage = value; db.commit()
        try: await update.message.delete()
        except Exception: pass
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text=f"🧮 <b>Risco & Tamanho</b>\n✅ Alavancagem máxima salva: <b>{value}x</b>", reply_markup=risk_menu_keyboard(user), parse_mode="HTML")
    except ValueError:
        try: await update.message.delete()
        except Exception: pass
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="❌ Não entendi. Envie um número inteiro (ex.: 10).")
        return ASKING_MAX_LEVERAGE
    except Exception as e:
        db.rollback(); logger.error(f"[settings] max_leverage: {e}", exc_info=True)
        try: await update.message.delete()
        except Exception: pass
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="Erro ao salvar. Tente novamente.")
        return ASKING_MAX_LEVERAGE
    finally:
        db.close()
    return ConversationHandler.END


async def receive_min_confidence(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_id_to_edit = context.user_data.get('settings_message_id')
    text = (update.message.text or "").strip().replace("%", "").replace(",", ".")
    db = SessionLocal()
    try:
        value = float(text)
        if value < 0 or value > 100:
            try: await update.message.delete()
            except Exception: pass
            await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="❌ Valor inválido. Envie um número entre 0 e 100 (ex.: 70).")
            return ASKING_MIN_CONFIDENCE
        user = db.query(User).filter_by(telegram_id=update.effective_user.id).first()
        if not user:
            await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="Usuário não encontrado. Use /start para registrar.")
            return ConversationHandler.END
        user.min_confidence = value; db.commit()
        try: await update.message.delete()
        except Exception: pass
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text=f"🧮 <b>Risco & Tamanho</b>\n✅ Confiança mínima salva: <b>{value:.1f}%</b>", reply_markup=risk_menu_keyboard(user), parse_mode="HTML")
    except ValueError:
        try: await update.message.delete()
        except Exception: pass
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="❌ Não entendi. Envie um número (ex.: 70).")
        return ASKING_MIN_CONFIDENCE
    except Exception as e:
        db.rollback(); logger.error(f"[settings] min_confidence: {e}", exc_info=True)
        try: await update.message.delete()
        except Exception: pass
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="Erro ao salvar. Tente novamente.")
        return ASKING_MIN_CONFIDENCE
    finally:
        db.close()
    return ConversationHandler.END

    
def _current_strategy_value(user) -> str:
    return (str(getattr(user, "stop_strategy", None) or
                getattr(user, "stop_strategy_mode", None) or
                getattr(user, "stop_strategy_type", None) or "breakeven")).lower()

def _next_strategy_value(value: str) -> str:
    return "trailing" if value.startswith("b") else "breakeven"

def _stopgain_summary(user) -> str:
    trigger = float(getattr(user, 'stop_gain_trigger_pct', 0) or 0)
    lock    = float(getattr(user, 'stop_gain_lock_pct', 0) or 0)
    cur     = _current_strategy_value(user)
    label   = "Breakeven" if cur.startswith("b") else "Trailing"
    return f"• Estratégia: {label}  |  Gatilho: {trigger:.2f}%  |  Trava: {lock:.2f}%"

async def toggle_stop_strategy_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == query.from_user.id).first()
        if not user:
            await query.edit_message_text("Não encontrei seu usuário. Use /start para registrar.")
            return
        cur = _current_strategy_value(user)
        nxt = _next_strategy_value(cur)

        if hasattr(user, "stop_strategy"):
            user.stop_strategy = nxt
        elif hasattr(user, "stop_strategy_mode"):
            user.stop_strategy_mode = nxt
        elif hasattr(user, "stop_strategy_type"):
            user.stop_strategy_type = nxt
        else:
            setattr(user, "stop_strategy", nxt)

        db.commit()
        header = ("🛡️ <b>Stop-Gain</b>\n<i>Configure estratégia, gatilho e trava.</i>\n\n"
                  f"{_stopgain_summary(user)}")
        await query.edit_message_text(text=header,
                                      reply_markup=stopgain_menu_keyboard(user),
                                      parse_mode="HTML")
    except Exception as e:
        db.rollback()
        logger.error(f"[settings] toggle_stop_strategy_handler erro: {e}", exc_info=True)
        await query.edit_message_text("Erro ao alternar estratégia.")
    finally:
        db.close()
    
async def execute_manual_close_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lida com a EXECUÇÃO do fechamento manual, editando a mensagem original."""
    query = update.callback_query
    await query.answer("Processando fechamento...")

    trade_id = int(query.data.split('_')[-1])
    user_id = update.effective_user.id

    db = SessionLocal()
    try:
        trade_to_close = db.query(Trade).filter_by(id=trade_id, user_telegram_id=user_id).first()

        if not trade_to_close:
            await query.edit_message_text("Erro: Trade não encontrado ou já fechado.")
            return

        user = db.query(User).filter_by(telegram_id=user_id).first()
        api_key = decrypt_data(user.api_key_encrypted)
        api_secret = decrypt_data(user.api_secret_encrypted)

        price_result = await get_market_price(trade_to_close.symbol)
        current_price = price_result["price"] if price_result.get("success") else trade_to_close.entry_price

        # Quantidade a fechar: usa restante se existir; caso contrário, a total
        qty_to_close = trade_to_close.remaining_qty if trade_to_close.remaining_qty is not None else trade_to_close.qty
        # Define o índice da posição conforme o lado (1 = LONG/Buy, 2 = SHORT/Sell)
        position_idx_to_close = 1 if (trade_to_close.side or "").upper() == 'LONG' else 2

        close_result = await close_partial_position(
            api_key,
            api_secret,
            trade_to_close.symbol,
            qty_to_close,
            trade_to_close.side,
            position_idx_to_close,
        )

        if close_result.get("success"):
            pnl_qty = qty_to_close
            pnl = (current_price - trade_to_close.entry_price) * pnl_qty if trade_to_close.side == 'LONG' else (trade_to_close.entry_price - current_price) * pnl_qty

            trade_to_close.status = 'CLOSED_MANUAL'
            trade_to_close.closed_at = func.now()
            trade_to_close.closed_pnl = pnl
            db.commit()

            resultado_str = "LUCRO" if pnl >= 0 else "PREJUÍZO"
            emoji = "✅" if pnl >= 0 else "🔻"
            # Cabeçalho claro: LUCRO / PREJUÍZO
            sign = "+" if pnl >= 0 else ""
            message_text = (
                f"{emoji} <b>{resultado_str}</b> — <b>{trade_to_close.symbol}</b> {trade_to_close.side}\n"
                f"• Tipo: <b>Fechamento manual</b>\n"
                f"• Quantidade: <b>{pnl_qty:g}</b>\n"
                f"• Entrada: <b>${trade_to_close.entry_price:,.4f}</b>\n"
                f"• Saída: <b>${current_price:,.4f}</b>\n"
                f"• P/L: <b>{sign}${abs(pnl):,.2f}</b>"
            )

            # --- LÓGICA DE EDIÇÃO APLICADA AQUI ---
            if trade_to_close.notification_message_id:
                try:
                    await context.bot.edit_message_text(
                        chat_id=user_id,
                        message_id=trade_to_close.notification_message_id,
                        text=message_text,
                        parse_mode='HTML'
                    )
                except BadRequest as e:
                    logger.warning(f"Não foi possível editar msg de fechamento manual para trade {trade_to_close.id}: {e}")
                    # Fallback: se não conseguir editar, envia uma nova mensagem.
                    await context.bot.send_message(chat_id=user_id, text=message_text, parse_mode='HTML')
            else:
                # Fallback para trades antigos sem ID de mensagem.
                await query.edit_message_text(message_text, parse_mode='HTML')

            await asyncio.sleep(2)
            await my_positions_handler(update, context) # Recarrega a lista de posições
        else:
            error_msg = close_result.get('error')
            await context.bot.send_message(
                chat_id=user_id,
                text=f"❌ Erro ao fechar a posição para {trade_to_close.symbol}: {error_msg}"
            )
            await my_positions_handler(update, context) # Recarrega a lista mesmo em caso de erro
    finally:
        db.close()


async def bot_config_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exibe o menu de configuração do bot com o modo de aprovação atual."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    
    db = SessionLocal()
    try:
        user = get_user_by_id(user_id)
        if user:
            await query.edit_message_text(
                "<b>🤖 Configuração do Bot</b>\n\n"
                "Ajuste o comportamento geral do bot.",
                parse_mode='HTML',
                reply_markup=bot_config_keyboard(user)
            )
    finally:
        db.close()

async def toggle_approval_mode_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Alterna o modo de aprovação de ordens entre Manual e Automático."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == user_id).first()
        
        if user:
            if user.approval_mode == 'AUTOMATIC':
                user.approval_mode = 'MANUAL'
            else:
                user.approval_mode = 'AUTOMATIC'
            
            db.commit() 
            
            try:
                await query.edit_message_text(
                    "<b>🤖 Configuração do Bot</b>\n\n"
                    "Ajuste o comportamento geral do bot.",
                    parse_mode='HTML',
                    reply_markup=bot_config_keyboard(user)
                )
            except BadRequest as e:
                if "Message is not modified" in str(e):
                    pass
                else:
                    logger.error(f"Erro ao editar mensagem em toggle_approval_mode: {e}")
    finally:
        db.close()

async def handle_signal_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lida com a aprovação ou rejeição de um sinal por um usuário específico."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    action, _, signal_id_str = query.data.partition('_signal_')
    signal_id = int(signal_id_str)
    
    db = SessionLocal()
    try:
        # Busca o sinal pendente para ESTE usuário específico
        signal_to_process = db.query(SignalForApproval).filter_by(id=signal_id, user_telegram_id=user_id).first()
        if not signal_to_process:
            await query.edit_message_text("Este sinal já foi processado ou expirou.")
            return

        user = db.query(User).filter_by(telegram_id=user_id).first()
        signal_data = signal_to_process.signal_data
        
        if action == 'approve':
            await query.edit_message_text("✅ **Entrada Aprovada!** Posicionando sua ordem...")
            
            # Executa o trade apenas para este usuário
            if signal_data.get("type") == SignalType.MARKET:
                await _execute_trade(signal_data, user, context.application, db, signal_to_process.source_name)
            elif signal_data.get("type") == SignalType.LIMIT:
                await _execute_limit_order_for_user(signal_data, user, context.application, db)
            
        elif action == 'reject':
            await query.edit_message_text("❌ **Entrada Rejeitada.** O sinal foi descartado.")
        
        db.delete(signal_to_process)
        db.commit()
    finally:
        db.close()

# --- FLUXO DE CONFIGURAÇÃO DE METAS DIÁRIAS ---

async def ask_profit_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Pergunta ao usuário a nova meta de lucro diário."""
    query = update.callback_query
    await query.answer()
    
    context.user_data['settings_message_id'] = query.message.message_id
    
    await query.edit_message_text(
        "Envie a sua meta de **lucro diário** em USDT.\n"
        "O bot irá parar de abrir novas ordens quando o lucro do dia atingir este valor.\n\n"
        "Envie apenas o número (ex: `100` para $100) ou `0` para desativar.",
        parse_mode='Markdown'
    )
    return ASKING_PROFIT_TARGET

async def receive_profit_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe, valida e salva a nova meta de lucro."""
    user_id = update.effective_user.id
    message_id_to_edit = context.user_data.get('settings_message_id')

    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=update.message.message_id)

    try:
        target_value = float(update.message.text.replace(',', '.'))
        if target_value < 0:
            raise ValueError("Valor não pode ser negativo")

        db = SessionLocal()
        try:
            user = db.query(User).filter_by(telegram_id=user_id).first()
            user.daily_profit_target = target_value
            db.commit()
            
            feedback_text = f"✅ Meta de lucro diário atualizada para ${target_value:.2f}."
            if target_value == 0:
                feedback_text = "✅ Meta de lucro diário foi desativada."

            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=message_id_to_edit,
                text=f"{feedback_text}\n\nAjuste outra configuração ou volte.",
                reply_markup=bot_config_keyboard(user)
            )
        finally:
            db.close()

    except (ValueError, TypeError):
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=message_id_to_edit,
            text="❌ Valor inválido. Por favor, tente novamente com um número (ex: 100)."
        )
        return ASKING_PROFIT_TARGET

    return ConversationHandler.END

# --- TP Strategy: presets ---
async def show_tp_presets_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        text=(
            "🎯 <b>Estratégias de Take Profit</b>\n\n"
            "Escolha um preset ou personalize sua distribuição."
        ),
        parse_mode='HTML',
        reply_markup=tp_presets_keyboard()
    )

async def set_tp_preset_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    token = query.data.split('set_tp_preset_')[-1]
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == query.from_user.id).first()
        if not user:
            await query.edit_message_text("Usuário não encontrado. Use /start.")
            return
        allowed = {"EQUAL", "FRONT_HEAVY", "BACK_HEAVY", "EXP_FRONT"}
        if token not in allowed:
            await query.edit_message_text("Preset inválido.")
            return
        user.tp_distribution = token
        db.commit()
        await show_tp_strategy_menu_handler(update, context)
    finally:
        db.close()

async def ask_loss_limit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Pergunta ao usuário o novo limite de perda diário."""
    query = update.callback_query
    await query.answer()
    
    context.user_data['settings_message_id'] = query.message.message_id
    
    await query.edit_message_text(
        "Envie o seu limite de **perda diária** em USDT.\n"
        "O bot irá parar de abrir novas ordens se a perda do dia atingir este valor.\n\n"
        "Envie um número positivo (ex: `50` para um limite de $50) ou `0` para desativar.",
        parse_mode='Markdown'
    )
    return ASKING_LOSS_LIMIT

async def receive_loss_limit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe, valida e salva o novo limite de perda."""
    user_id = update.effective_user.id
    message_id_to_edit = context.user_data.get('settings_message_id')

    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=update.message.message_id)

    try:
        limit_value = float(update.message.text.replace(',', '.'))
        if limit_value < 0:
            raise ValueError("Valor não pode ser negativo")

        db = SessionLocal()
        try:
            user = db.query(User).filter_by(telegram_id=user_id).first()
            user.daily_loss_limit = limit_value
            db.commit()

            feedback_text = f"✅ Limite de perda diário atualizado para ${limit_value:.2f}."
            if limit_value == 0:
                feedback_text = "✅ Limite de perda diário foi desativado."

            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=message_id_to_edit,
                text=f"{feedback_text}\n\nAjuste outra configuração ou volte.",
                reply_markup=bot_config_keyboard(user)
            )
        finally:
            db.close()

    except (ValueError, TypeError):
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=message_id_to_edit,
            text="❌ Valor inválido. Por favor, tente novamente com um número positivo (ex: 50)."
        )
        return ASKING_LOSS_LIMIT

    return ConversationHandler.END

# --- MENU DE DESEMPENHO ---

async def receive_pending_expiry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe e salva o tempo de expiração de ordens pendentes (minutos)."""
    user_id = update.effective_user.id
    message_id_to_edit = context.user_data.get('settings_message_id')
    try:
        await update.message.delete()
    except Exception:
        pass

    try:
        raw = (update.message.text or '').strip().replace(',', '.')
        minutes = int(float(raw))
        if minutes < 0:
            raise ValueError("negativo")

        db = SessionLocal()
        try:
            user = db.query(User).filter_by(telegram_id=user_id).first()
            if not user:
                raise ValueError("user not found")
            user.pending_expiry_minutes = minutes
            db.commit()

            feedback = (
                f"✅ Expiração de pendentes definida para <b>{minutes} min</b>."
                if minutes > 0 else
                "✅ Expiração de pendentes <b>desativada</b>."
            )
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=message_id_to_edit,
                text=f"{feedback}\n\nAjuste outra configuração ou volte.",
                parse_mode='HTML',
                reply_markup=bot_config_keyboard(user)
            )
        finally:
            db.close()
    except Exception:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=message_id_to_edit,
            text="❌ Valor inválido. Envie um inteiro >= 0 (ex.: 0, 60, 120)."
        )
        return ASKING_PENDING_EXPIRY_MINUTES

    return ConversationHandler.END

async def performance_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exibe o painel de desempenho e lida com a seleção de período, usando o fuso horário de SP."""
    query = update.callback_query
    from telegram.error import TimedOut
    try:
        await query.answer()
    except BadRequest as e:
        # callback antigo/expirado: não faz nada e evita stacktrace
        logger.warning(f"[perf] callback expirado/antigo: {e}")
        return
    except TimedOut as e:
        logger.warning(f"[perf] query.answer timeout: {e} — seguindo fluxo mesmo assim")

    user_id = query.from_user.id
    
    # --- LÓGICA DE FUSO HORÁRIO CORRIGIDA ---
    br_timezone = pytz.timezone("America/Sao_Paulo")
    now_br = datetime.now(br_timezone)
    
    callback_data = query.data
    start_dt, end_dt = None, None

    if callback_data == 'perf_today':
        start_dt = now_br.replace(hour=0, minute=0, second=0, microsecond=0)
        end_dt = now_br
    elif callback_data == 'perf_yesterday':
        yesterday = now_br.date() - timedelta(days=1)
        start_dt = br_timezone.localize(datetime.combine(yesterday, time.min))
        end_dt = br_timezone.localize(datetime.combine(yesterday, time.max))
    elif callback_data == 'perf_7_days':
        start_dt = (now_br - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
        end_dt = now_br
    elif callback_data == 'perf_30_days':
        start_dt = (now_br - timedelta(days=29)).replace(hour=0, minute=0, second=0, microsecond=0)
        end_dt = now_br

    if start_dt and end_dt:
        await query.edit_message_text(
            text="⏳ Calculando desempenho para o período selecionado...",
            reply_markup=performance_menu_keyboard()
        )
        
        report_text = await generate_performance_report(user_id, start_dt, end_dt)
        
        await query.edit_message_text(
            text=report_text,
            parse_mode='HTML',
            reply_markup=performance_menu_keyboard()
        )

# --- FLUXO DE CONFIGURAÇÃO DE WHITELIST ---

async def ask_coin_whitelist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prompt para editar Whitelist com instruções, categorias e o valor atual."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    query = update.callback_query
    await query.answer()

    context.user_data['settings_message_id'] = query.message.message_id
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == query.from_user.id).first()
        if not user:
            await query.edit_message_text("Não encontrei seu usuário. Use /start para registrar.")
            return ASKING_COIN_WHITELIST # Permanece no estado, mas informa o erro
            
        # COMENTÁRIO: Lógica adicionada para buscar a configuração atual do usuário.
        current_whitelist = getattr(user, 'coin_whitelist', 'todas') or 'todas'

        text = (
            f"✅ <b>Whitelist de Moedas</b>\n\n"
            f"⚙️ <b>Sua Configuração Atual:</b>\n<code>{current_whitelist}</code>\n\n"
            "Você pode definir exatamente <i>quais moedas</i> o bot poderá operar.\n\n"
            "🧩 <b>Como usar</b>\n"
            "• Digite tickers separados por vírgula (ex.: <code>BTCUSDT,ETHUSDT,SOLUSDT</code>)\n"
            "• Pode misturar <b>tickers</b> com <b>categorias</b>\n"
            "• Use <code>todas</code> para liberar todos os pares\n\n"
            "📦 <b>Categorias disponíveis</b>\n"
            "• <b>bluechips</b> → BTC, ETH, BNB\n"
            "• <b>altcoins</b> → SOL, XRP, ADA, AVAX, DOT, MATIC, LINK...\n"
            "• <b>defi</b> → UNI, AAVE, MKR, SNX, COMP, CRV...\n"
            "• <b>infra</b> → LINK, GRT, FIL\n"
            "• <b>memecoins</b> → DOGE, SHIB, PEPE, WIF, FLOKI, BONK\n\n"
            "ℹ️ Exemplos válidos:\n"
            "• <code>bluechips</code>\n"
            "• <code>memecoins,altcoins</code>\n"
            "• <code>BTCUSDT,ETHUSDT,defi</code>\n\n"
            "⬅️ Clique em <b>Voltar</b> para cancelar sem alterações."
        )

        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Voltar", callback_data="back_to_settings_menu")]
        ])

        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    finally:
        db.close()
        
    return ASKING_COIN_WHITELIST

async def receive_coin_whitelist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Salva whitelist enviada pelo usuário e retorna ao menu de Configurações.
    Aceita tickers (ex.: BTCUSDT) e keywords de categorias (ex.: bluechips, defi, memecoins, infra, altcoins).
    """
    text = (update.message.text or "").strip()
    message_id_to_edit = context.user_data.get('settings_message_id')
    db = SessionLocal()
    try:
        if not text:
            try: await update.message.delete()
            except Exception: pass
            await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit,
                                               text="❌ Envie ao menos 1 ticker ou categoria. Ex.: BTCUSDT,ETHUSDT ou bluechips")
            return ASKING_COIN_WHITELIST

        # Normalização básica
        raw_items = [i.strip().upper() for i in text.split(",") if i.strip()]
        unique_items = []
        seen = set()
        for i in raw_items:
            if i not in seen:
                unique_items.append(i)
                seen.add(i)

        # Dica: não expandimos categorias na string salva; mantemos como o usuário enviou.
        # A checagem em tempo de execução usa core.whitelist_service.is_coin_in_whitelist(...)
        normalized = ",".join(unique_items)

        user = db.query(User).filter(User.telegram_id == update.effective_user.id).first()
        if not user:
            await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit,
                                               text="Usuário não encontrado. Use /start para registrar.")
            return ConversationHandler.END

        # Se seu modelo for user.coin_whitelist_str ou similar, ajuste o campo aqui:
        if hasattr(user, "coin_whitelist"):
            user.coin_whitelist = normalized
        elif hasattr(user, "coin_whitelist_str"):
            user.coin_whitelist_str = normalized
        else:
            # cria atributo em runtime para evitar quebra; ideal é usar o nome real do seu modelo
            setattr(user, "coin_whitelist", normalized)

        db.commit()

        # Apaga a mensagem do usuário para manter a timeline limpa (se possível)
        try:
            await update.message.delete()
        except Exception:
            pass

        # Mensagem de confirmação + retorno ao menu raiz de Configurações
        header = (
            "⚙️ <b>Configurações de Trade</b>\n"
            "<i>Whitelist atualizada com sucesso.</i>\n\n"
            f"📦 <b>Lista salva</b>: <code>{normalized}</code>"
        )
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=message_id_to_edit,
            text=header,
            reply_markup=settings_menu_keyboard(user),
            parse_mode="HTML",
        )
    except Exception as e:
        db.rollback()
        logger.error(f"[settings] receive_coin_whitelist erro: {e}", exc_info=True)
        try: await update.message.delete()
        except Exception: pass
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="Erro ao salvar a whitelist. Tente novamente.")
        return ASKING_COIN_WHITELIST
    finally:
        db.close()
    return ConversationHandler.END

async def list_closed_trades_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Busca no DB e lista os últimos trades fechados do usuário.
    Ajustado para priorizar PnL quando existir e registrar telemetria de render por item.
    """
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    logger.debug("[histórico] start user_id=%s", user_id)
    await query.edit_message_text("Buscando seu histórico de trades...")

    db = SessionLocal()
    try:
        closed_trades = db.query(Trade).filter(
            Trade.user_telegram_id == user_id,
            Trade.status.like('%CLOSED%')
        ).order_by(Trade.closed_at.desc()).limit(15).all()

        logger.info("[histórico] encontrados=%d user_id=%s", len(closed_trades), user_id)

        message = "<b>📜 Seus Últimos Trades Fechados</b>\n\n"

        if not closed_trades:
            message += "Nenhum trade fechado encontrado no seu histórico."
        else:
            import pytz
            br_tz = pytz.timezone("America/Sao_Paulo")
            utc = pytz.utc
            for trade in closed_trades:
                if trade.closed_at:
                    dt = trade.closed_at
                    try:
                        if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
                            dt = utc.localize(dt)
                        data_fechamento = dt.astimezone(br_tz).strftime('%d/%m %H:%M')
                    except Exception:
                        data_fechamento = trade.closed_at.strftime('%d/%m %H:%M')
                else:
                    data_fechamento = 'N/A'

                render_mode = "fallback_externo"
                if trade.closed_pnl is not None:
                    try:
                        pnl_val = float(trade.closed_pnl)
                        emoji = "🏆" if pnl_val >= 0 else "🛑"
                        label = "Lucro (líquido)" if pnl_val >= 0 else "Prejuízo (líquido)"
                        resultado_str = f"{emoji} <b>{label}: ${pnl_val:,.2f}</b>"
                        render_mode = "via_pnl"
                    except Exception:
                        status_upper = (trade.status or "").upper()
                        if "PROFIT" in status_upper:
                            emoji, resultado_str = "🏆", "<b>Resultado:</b> lucro"
                            render_mode = "via_status"
                        elif "LOSS" in status_upper or "STOP" in status_upper:
                            emoji, resultado_str = "🛑", "<b>Resultado:</b> prejuízo"
                            render_mode = "via_status"
                        elif "MANUAL" in status_upper:
                            emoji, resultado_str = "✅", "<i>Fechado manualmente</i>"
                            render_mode = "via_status"
                        else:
                            emoji, resultado_str = "ℹ️", "<i>Fechado externamente</i>"
                else:
                    status_upper = (trade.status or "").upper()
                    if "PROFIT" in status_upper:
                        emoji, resultado_str = "🏆", "<b>Resultado:</b> lucro"
                        render_mode = "via_status"
                    elif "LOSS" in status_upper or "STOP" in status_upper:
                        emoji, resultado_str = "🛑", "<b>Resultado:</b> prejuízo"
                        render_mode = "via_status"
                    elif "MANUAL" in status_upper:
                        emoji, resultado_str = "✅", "<i>Fechado manualmente</i>"
                        render_mode = "via_status"
                    else:
                        emoji, resultado_str = "ℹ️", "<i>Fechado externamente</i>"

                logger.debug(
                    "[histórico:item] user_id=%s trade_id=%s symbol=%s status=%s closed_pnl=%s modo=%s",
                    user_id, getattr(trade, "id", None), trade.symbol, trade.status, str(trade.closed_pnl), render_mode
                )

                message += (
                    f"{emoji} <b>{trade.symbol}</b> ({trade.side})\n"
                    f"  - Fechado em: {data_fechamento}\n"
                    f"  - {resultado_str}\n\n"
                )

        keyboard = [[InlineKeyboardButton("⬅️ Voltar ao Desempenho", callback_data='perf_today')]]
        
        await query.edit_message_text(
            text=message,
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

        logger.debug("[histórico] end user_id=%s", user_id)

    finally:
        db.close()

async def prompt_manual_close_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exibe a tela de confirmação para o fechamento manual de uma posição."""
    query = update.callback_query
    await query.answer()
    
    # COMENTÁRIO: O callback agora é `confirm_close_<trade_id>`
    trade_id_str = query.data.split('_')[-1]
    if not trade_id_str.isdigit():
        # Lida com o formato antigo `confirm_close_group|SYMBOL|SIDE` como fallback
        await query.edit_message_text("Este botão é de uma versão antiga. Por favor, volte e abra o menu de posições novamente.")
        return

    trade_id = int(trade_id_str)
    
    db = SessionLocal()
    try:
        trade = db.query(Trade).filter_by(id=trade_id).first()
        if not trade:
            await query.edit_message_text("Erro: Trade não encontrado ou já fechado.")
            return

        message = (
            f"⚠️ <b>Confirmar Fechamento</b> ⚠️\n\n"
            f"Você tem certeza que deseja fechar manualmente sua posição em <b>{trade.symbol}</b> (ID: {trade.id})?\n\n"
            f"Esta ação é irreversível."
        )
        await query.edit_message_text(
            text=message,
            parse_mode='HTML',
            reply_markup=confirm_manual_close_keyboard(trade_id)
        )
    finally:
        db.close()

async def toggle_bot_status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Liga/Desliga o bot ou ativa o modo dormir, em um ciclo de 3 estados."""
    query = update.callback_query
    user_id = update.effective_user.id

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == user_id).first()
        if not user:
            await query.answer("Usuário não encontrado.", show_alert=True)
            return

        # Lógica do ciclo de 3 estados
        alert_message = ""
        if not user.is_active:
            # ESTADO ATUAL: Pausado -> PRÓXIMO: Ativo 24h
            user.is_active = True
            user.is_sleep_mode_enabled = False
            alert_message = "Bot ATIVADO."
            
        elif user.is_active and not user.is_sleep_mode_enabled:
            # ESTADO ATUAL: Ativo 24h -> PRÓXIMO: Ativo com Modo Dormir
            user.is_sleep_mode_enabled = True
            alert_message = "Modo Dormir ATIVADO. O bot pausará entre 00:00 e 07:00."

        else: # user.is_active and user.is_sleep_mode_enabled
            # ESTADO ATUAL: Ativo com Modo Dormir -> PRÓXIMO: Pausado
            user.is_active = False
            user.is_sleep_mode_enabled = False # Reseta o modo dormir ao pausar
            alert_message = "Bot PAUSADO."

            # Mantém a lógica de cancelar ordens pendentes ao pausar
            api_key = decrypt_data(user.api_key_encrypted)
            api_secret = decrypt_data(user.api_secret_encrypted)
            pendentes = db.query(PendingSignal).filter_by(user_telegram_id=user_id).all()
            canceladas = 0
            for p in pendentes:
                try:
                    resp = await cancel_order(api_key, api_secret, p.order_id, p.symbol)
                    if not resp.get("success"):
                        logger.warning(f"[PAUSE] Falha ao cancelar ordem {p.order_id} ({p.symbol}): {resp.get('error')}")
                    db.delete(p)
                    canceladas += 1
                except Exception as e:
                    logger.error(f"[PAUSE] Exceção ao cancelar {p.order_id} ({p.symbol}): {e}", exc_info=True)
            
            if canceladas > 0:
                alert_message += f" {canceladas} ordem(ns) pendente(s) foi(ram) cancelada(s)."
        
        db.commit()
        await query.answer(alert_message, show_alert=True)

        # Re-renderiza a tela de Configuração do Bot (toggle foi movido para lá)
        try:
            await query.edit_message_text(
                text="<b>🤖 Configuração do Bot</b>\n\nAjuste o comportamento geral do bot.",
                parse_mode='HTML',
                reply_markup=bot_config_keyboard(user)
            )
        except BadRequest as e:
            logger.warning(f"Falha ao atualizar menu do bot após toggle: {e}")

    finally:
        db.close()

async def ask_entry_percent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("📥 Envie o <b>tamanho de entrada</b> em % (ex.: 3.5)", parse_mode="HTML")
    return ASKING_ENTRY_PERCENT

async def ask_max_leverage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⚙️ Envie a <b>alavancagem máxima</b> (ex.: 5, 10, 20)", parse_mode="HTML")
    return ASKING_MAX_LEVERAGE

async def ask_min_confidence(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("🎯 Envie a <b>confiança mínima</b> em % (ex.: 70)", parse_mode="HTML")
    return ASKING_MIN_CONFIDENCE

async def ask_stop_gain_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("🚀 Envie o <b>gatilho</b> do Stop-Gain em % (ex.: 3)", parse_mode="HTML")
    return ASKING_STOP_GAIN_TRIGGER

async def ask_stop_gain_lock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("🔒 Envie a <b>trava</b> do Stop-Gain em % (ex.: 1)", parse_mode="HTML")
    return ASKING_STOP_GAIN_LOCK

async def ask_be_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("🎯 Envie o <b>gatilho opcional</b> do Break‑Even por PnL em % (ex.: 2). Use 0 para desativar.", parse_mode="HTML")
    return ASKING_BE_TRIGGER

async def ask_ts_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("📈 Envie o <b>gatilho opcional</b> do Trailing Stop por PnL em % (ex.: 3). Use 0 para desativar.", parse_mode="HTML")
    return ASKING_TS_TRIGGER

async def ask_pending_expiry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Pergunta o tempo (em minutos) para expirar ordens pendentes."""
    query = update.callback_query
    await query.answer()
    context.user_data['settings_message_id'] = query.message.message_id
    await query.edit_message_text(
        "Envie o <b>tempo em minutos</b> para expirar ordens <i>pendentes</i>.\n\n"
        "- Envie <code>0</code> para desativar.\n"
        "- Ex.: <code>120</code> para 2 horas.",
        parse_mode='HTML'
    )
    return ASKING_PENDING_EXPIRY_MINUTES

async def ask_circuit_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['settings_message_id'] = query.message.message_id
    await query.edit_message_text("⚡ Envie o <b>limite</b> do disjuntor (inteiro, ex.: 3)", parse_mode="HTML")
    return ASKING_CIRCUIT_THRESHOLD

async def ask_circuit_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['settings_message_id'] = query.message.message_id
    await query.edit_message_text("⏸️ Envie a <b>pausa</b> após disparo (minutos, ex.: 120)", parse_mode="HTML")
    return ASKING_CIRCUIT_PAUSE

# ---- Stop Inicial (UI) ----
async def show_initial_stop_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = get_user_by_id(update.effective_user.id)
    # Mensagem mais informativa com detalhes do modo atual
    mode = (getattr(user, 'initial_sl_mode', 'ADAPTIVE') or 'ADAPTIVE').upper()
    entry_pct = float(getattr(user, 'entry_size_percent', 0) or 0)
    lev = float(getattr(user, 'max_leverage', 0) or 0)
    risk = float(getattr(user, 'risk_per_trade_pct', 1.0) or 1.0)
    info = ""
    if mode == 'FIXED':
        info = "Usa um percentual fixo sobre o preço de entrada."
    elif mode in ('FOLLOW', 'FOLLOW_SIGNAL', 'SIGNAL'):
        info = "Segue o SL informado no sinal (apenas alinhado ao tick)."
    else:
        # ADAPTIVE
        try:
            max_sl = (risk / 100.0) / ((entry_pct / 100.0) * lev) * 100.0 if entry_pct > 0 and lev > 0 else None
        except Exception:
            max_sl = None
        if max_sl is not None:
            info = (
                "Limita a distância do SL para respeitar o seu risco por trade (% do equity).\n"
                f"Fórmula: sl% ≤ risco% / (entrada% × alavancagem) → ~{max_sl:.2f}% agora."
            )
        else:
            info = (
                "Limita a distância do SL para respeitar o seu risco por trade (% do equity).\n"
                "Defina 'Tamanho de Entrada' e 'Alavancagem' para ver o limite estimado."
            )
    header = (
        "🛑 <b>Stop Inicial</b>\n\n"
        f"{info}"
    )
    await query.edit_message_text(text=header, parse_mode='HTML', reply_markup=initial_stop_menu_keyboard(user))

async def toggle_initial_sl_mode_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == query.from_user.id).first()
        if not user:
            await query.edit_message_text("Usuário não encontrado. Use /start para registrar.")
            return
        cur = (getattr(user, 'initial_sl_mode', 'ADAPTIVE') or 'ADAPTIVE').upper()
        # Ciclo de 3 estados: ADAPTIVE -> FOLLOW_SIGNAL -> FIXED -> ADAPTIVE
        if cur == 'ADAPTIVE':
            nxt = 'FOLLOW_SIGNAL'
        elif cur in ('FOLLOW', 'FOLLOW_SIGNAL', 'SIGNAL'):
            nxt = 'FIXED'
        else:
            nxt = 'ADAPTIVE'
        user.initial_sl_mode = nxt
        db.commit()
        # Recalcula header com base no modo após alternar
        mode = (getattr(user, 'initial_sl_mode', 'ADAPTIVE') or 'ADAPTIVE').upper()
        entry_pct = float(getattr(user, 'entry_size_percent', 0) or 0)
        lev = float(getattr(user, 'max_leverage', 0) or 0)
        risk = float(getattr(user, 'risk_per_trade_pct', 1.0) or 1.0)
        if mode == 'FIXED':
            info = "Usa um percentual fixo sobre o preço de entrada."
        elif mode in ('FOLLOW', 'FOLLOW_SIGNAL', 'SIGNAL'):
            info = "Segue o SL informado no sinal (apenas alinhado ao tick)."
        else:
            try:
                max_sl = (risk / 100.0) / ((entry_pct / 100.0) * lev) * 100.0 if entry_pct > 0 and lev > 0 else None
            except Exception:
                max_sl = None
            if max_sl is not None:
                info = (
                    "Limita a distância do SL para respeitar o seu risco por trade (% do equity).\n"
                    f"Fórmula: sl% ≤ risco% / (entrada% × alavancagem) → ~{max_sl:.2f}% agora."
                )
            else:
                info = (
                    "Limita a distância do SL para respeitar o seu risco por trade (% do equity).\n"
                    "Defina 'Tamanho de Entrada' e 'Alavancagem' para ver o limite estimado."
                )
        header = (
            "🛑 <b>Stop Inicial</b>\n\n"
            f"{info}"
        )
        await query.edit_message_text(text=header, parse_mode='HTML', reply_markup=initial_stop_menu_keyboard(user))
    except Exception as e:
        db.rollback(); logger.error(f"[settings] toggle_initial_sl_mode erro: {e}", exc_info=True)
        await query.edit_message_text("Erro ao alternar o modo de Stop Inicial.")
    finally:
        db.close()

async def ask_initial_sl_fixed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['settings_message_id'] = query.message.message_id
    await query.edit_message_text("🛑 Envie o <b>percentual fixo</b> do Stop Inicial (ex.: 1.5)", parse_mode='HTML')
    return ASKING_INITIAL_SL_FIXED

async def receive_initial_sl_fixed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_id_to_edit = context.user_data.get('settings_message_id')
    text = (update.message.text or "").strip().replace('%', '').replace(',', '.')
    db = SessionLocal()
    try:
        value = float(text)
        if value <= 0 or value > 50:
            try: await update.message.delete()
            except Exception: pass
            await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="❌ Valor inválido. Envie > 0 e <= 50 (ex.: 1.5).")
            return ASKING_INITIAL_SL_FIXED
        user = db.query(User).filter_by(telegram_id=update.effective_user.id).first()
        if not user:
            await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="Usuário não encontrado. Use /start para registrar.")
            return ConversationHandler.END
        user.initial_sl_fixed_pct = value; db.commit()
        try: await update.message.delete()
        except Exception: pass
        header = (
            "🛑 <b>Stop Inicial</b>\n<i>Defina o SL inicial: Fixo (%) ou Adaptativo (risco por trade).</i>\n\n"
            f"✅ Percentual fixo salvo: <b>{value:.2f}%</b>"
        )
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text=header, reply_markup=initial_stop_menu_keyboard(user), parse_mode='HTML')
    except ValueError:
        try: await update.message.delete()
        except Exception: pass
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="❌ Não entendi. Envie um número (ex.: 1.5).")
        return ASKING_INITIAL_SL_FIXED
    except Exception as e:
        db.rollback(); logger.error(f"[settings] initial_sl_fixed_pct: {e}", exc_info=True)
        try: await update.message.delete()
        except Exception: pass
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="Erro ao salvar. Tente novamente.")
        return ASKING_INITIAL_SL_FIXED
    finally:
        db.close()
    return ConversationHandler.END

async def ask_risk_per_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['settings_message_id'] = query.message.message_id
    await query.edit_message_text("🛑 Envie o <b>risco por trade</b> em % do equity (ex.: 1)", parse_mode='HTML')
    return ASKING_RISK_PER_TRADE

async def receive_risk_per_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_id_to_edit = context.user_data.get('settings_message_id')
    text = (update.message.text or "").strip().replace('%', '').replace(',', '.')
    db = SessionLocal()
    try:
        value = float(text)
        if value <= 0 or value > 10:
            try: await update.message.delete()
            except Exception: pass
            await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="❌ Valor inválido. Envie > 0 e <= 10 (ex.: 1).")
            return ASKING_RISK_PER_TRADE
        user = db.query(User).filter_by(telegram_id=update.effective_user.id).first()
        if not user:
            await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="Usuário não encontrado. Use /start para registrar.")
            return ConversationHandler.END
        user.risk_per_trade_pct = value; db.commit()
        try: await update.message.delete()
        except Exception: pass
        header = (
            "🛑 <b>Stop Inicial</b>\n<i>Defina o SL inicial: Fixo (%) ou Adaptativo (risco por trade).</i>\n\n"
            f"✅ Risco por trade salvo: <b>{value:.2f}%</b>"
        )
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text=header, reply_markup=initial_stop_menu_keyboard(user), parse_mode='HTML')
    except ValueError:
        try: await update.message.delete()
        except Exception: pass
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="❌ Não entendi. Envie um número (ex.: 1).")
        return ASKING_RISK_PER_TRADE
    except Exception as e:
        db.rollback(); logger.error(f"[settings] risk_per_trade_pct: {e}", exc_info=True)
        try: await update.message.delete()
        except Exception: pass
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="Erro ao salvar. Tente novamente.")
        return ASKING_RISK_PER_TRADE
    finally:
        db.close()
    return ConversationHandler.END

# ---- STOP-GAIN ----
async def receive_stop_gain_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_id_to_edit = context.user_data.get('settings_message_id')
    text = (update.message.text or "").strip().replace("%", "").replace(",", ".")
    db = SessionLocal()
    try:
        value = float(text)
        if value < 0 or value > 100:
            try: await update.message.delete()
            except Exception: pass
            await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="❌ Valor inválido. Envie entre 0 e 100 (ex.: 3).")
            return ASKING_STOP_GAIN_TRIGGER
        user = db.query(User).filter_by(telegram_id=update.effective_user.id).first()
        if not user:
            await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="Usuário não encontrado. Use /start para registrar.")
            return ConversationHandler.END
        user.stop_gain_trigger_pct = value; db.commit()
        try: await update.message.delete()
        except Exception: pass
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text=f"🛡️ <b>Stop-Gain</b>\n✅ Gatilho salvo: <b>{value:.2f}%</b>", reply_markup=stopgain_menu_keyboard(user), parse_mode="HTML")
    except ValueError:
        try: await update.message.delete()
        except Exception: pass
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="❌ Não entendi. Envie um número (ex.: 3).")
        return ASKING_STOP_GAIN_TRIGGER
    except Exception as e:
        db.rollback(); logger.error(f"[settings] stop_gain_trigger_pct: {e}", exc_info=True)
        try: await update.message.delete()
        except Exception: pass
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="Erro ao salvar. Tente novamente.")
        return ASKING_STOP_GAIN_TRIGGER
    finally:
        db.close()
    return ConversationHandler.END


async def receive_stop_gain_lock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_id_to_edit = context.user_data.get('settings_message_id')
    text = (update.message.text or "").strip().replace("%", "").replace(",", ".")
    db = SessionLocal()
    try:
        value = float(text)
        if value < 0 or value > 100:
            try: await update.message.delete()
            except Exception: pass
            await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="❌ Valor inválido. Envie entre 0 e 100 (ex.: 1).")
            return ASKING_STOP_GAIN_LOCK
        user = db.query(User).filter_by(telegram_id=update.effective_user.id).first()
        if not user:
            await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="Usuário não encontrado. Use /start para registrar.")
            return ConversationHandler.END
        user.stop_gain_lock_pct = value; db.commit()
        try: await update.message.delete()
        except Exception: pass
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text=f"🛡️ <b>Stop-Gain</b>\n✅ Trava salva: <b>{value:.2f}%</b>", reply_markup=stopgain_menu_keyboard(user), parse_mode="HTML")
    except ValueError:
        try: await update.message.delete()
        except Exception: pass
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="❌ Não entendi. Envie um número (ex.: 1).")
        return ASKING_STOP_GAIN_LOCK
    except Exception as e:
        db.rollback(); logger.error(f"[settings] stop_gain_lock_pct: {e}", exc_info=True)
        try: await update.message.delete()
        except Exception: pass
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="Erro ao salvar. Tente novamente.")
        return ASKING_STOP_GAIN_LOCK
    finally:
        db.close()
    return ConversationHandler.END

async def receive_be_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_id_to_edit = context.user_data.get('settings_message_id')
    text = (update.message.text or "").strip().replace("%", "").replace(",", ".")
    db = SessionLocal()
    try:
        value = float(text)
        if value < 0 or value > 100:
            try: await update.message.delete()
            except Exception: pass
            await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="❌ Valor inválido. Envie entre 0 e 100 (ex.: 2).")
            return ASKING_BE_TRIGGER
        user = db.query(User).filter_by(telegram_id=update.effective_user.id).first()
        if not user:
            await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="Usuário não encontrado. Use /start para registrar.")
            return ConversationHandler.END
        # Persistência: atributo pode já existir na tabela; se não existir, SQL pode falhar. Exige migração.
        setattr(user, 'be_trigger_pct', value)
        db.commit()
        try: await update.message.delete()
        except Exception: pass
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text=f"🛡️ <b>Stop-Gain / BE</b>\n✅ Gatilho Break‑Even por PnL salvo: <b>{value:.2f}%</b>", reply_markup=stopgain_menu_keyboard(user), parse_mode="HTML")
    except ValueError:
        try: await update.message.delete()
        except Exception: pass
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="❌ Não entendi. Envie um número (ex.: 2).")
        return ASKING_BE_TRIGGER
    except Exception as e:
        db.rollback(); logger.error(f"[settings] be_trigger_pct: {e}", exc_info=True)
        try: await update.message.delete()
        except Exception: pass
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="Erro ao salvar. Tente novamente.")
        return ASKING_BE_TRIGGER
    finally:
        db.close()
    return ConversationHandler.END

async def receive_ts_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_id_to_edit = context.user_data.get('settings_message_id')
    text = (update.message.text or "").strip().replace("%", "").replace(",", ".")
    db = SessionLocal()
    try:
        value = float(text)
        if value < 0 or value > 100:
            try: await update.message.delete()
            except Exception: pass
            await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="❌ Valor inválido. Envie entre 0 e 100 (ex.: 3).")
            return ASKING_TS_TRIGGER
        user = db.query(User).filter_by(telegram_id=update.effective_user.id).first()
        if not user:
            await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="Usuário não encontrado. Use /start para registrar.")
            return ConversationHandler.END
        setattr(user, 'ts_trigger_pct', value)
        db.commit()
        try: await update.message.delete()
        except Exception: pass
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text=f"🛡️ <b>Stop-Gain / TS</b>\n✅ Gatilho Trailing por PnL salvo: <b>{value:.2f}%</b>", reply_markup=stopgain_menu_keyboard(user), parse_mode="HTML")
    except ValueError:
        try: await update.message.delete()
        except Exception: pass
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="❌ Não entendi. Envie um número (ex.: 3).")
        return ASKING_TS_TRIGGER
    except Exception as e:
        db.rollback(); logger.error(f"[settings] ts_trigger_pct: {e}", exc_info=True)
        try: await update.message.delete()
        except Exception: pass
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="Erro ao salvar. Tente novamente.")
        return ASKING_TS_TRIGGER
    finally:
        db.close()
    return ConversationHandler.END

# ---- DISJUNTOR ----
async def receive_circuit_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_id_to_edit = context.user_data.get('settings_message_id')
    text = (update.message.text or "").strip()
    db = SessionLocal()
    try:
        value = int(float(text))
        if value < 0 or value > 1000:
            try: await update.message.delete()
            except Exception: pass
            await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="❌ Valor inválido. Envie um inteiro entre 0 e 1000 (ex.: 3).")
            return ASKING_CIRCUIT_THRESHOLD
        user = db.query(User).filter_by(telegram_id=update.effective_user.id).first()
        if not user:
            await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="Usuário não encontrado. Use /start para registrar.")
            return ConversationHandler.END
        user.circuit_breaker_threshold = value; db.commit()
        try: await update.message.delete()
        except Exception: pass
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text=f"🚫 <b>Disjuntor</b>\n✅ Limite salvo: <b>{value}</b>", reply_markup=circuit_menu_keyboard(user), parse_mode="HTML")
    except ValueError:
        try: await update.message.delete()
        except Exception: pass
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="❌ Não entendi. Envie um número inteiro (ex.: 3).")
        return ASKING_CIRCUIT_THRESHOLD
    except Exception as e:
        db.rollback(); logger.error(f"[settings] circuit_breaker_threshold: {e}", exc_info=True)
        try: await update.message.delete()
        except Exception: pass
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="Erro ao salvar. Tente novamente.")
        return ASKING_CIRCUIT_THRESHOLD
    finally:
        db.close()
    return ConversationHandler.END

async def receive_circuit_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_id_to_edit = context.user_data.get('settings_message_id')
    text = (update.message.text or "").strip().lower().replace("min", "").replace("m", "")
    db = SessionLocal()
    try:
        value = int(float(text))
        if value < 0 or value > 1440:
            try: await update.message.delete()
            except Exception: pass
            await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="❌ Valor inválido. Envie um inteiro entre 0 e 1440 (ex.: 120).")
            return ASKING_CIRCUIT_PAUSE
        user = db.query(User).filter_by(telegram_id=update.effective_user.id).first()
        if not user:
            await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="Usuário não encontrado. Use /start para registrar.")
            return ConversationHandler.END
        user.circuit_breaker_pause_minutes = value; db.commit()
        try: await update.message.delete()
        except Exception: pass
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text=f"🚫 <b>Disjuntor</b>\n✅ Pausa salva: <b>{value} min</b>", reply_markup=circuit_menu_keyboard(user), parse_mode="HTML")
    except ValueError:
        try: await update.message.delete()
        except Exception: pass
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="❌ Não entendi. Envie um número inteiro (ex.: 120).")
        return ASKING_CIRCUIT_PAUSE
    except Exception as e:
        db.rollback(); logger.error(f"[settings] circuit_breaker_pause_minutes: {e}", exc_info=True)
        try: await update.message.delete()
        except Exception: pass
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="Erro ao salvar. Tente novamente.")
        return ASKING_CIRCUIT_PAUSE
    finally:
        db.close()
    return ConversationHandler.END

async def signal_filters_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exibe o menu de configuração de filtros de sinais."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(telegram_id=user_id).first()
        if user:
            await query.edit_message_text(
                "<b>🔬 Filtros de Análise Técnica</b>\n\n"
                "Ative e configure filtros para melhorar a qualidade dos sinais executados.",
                parse_mode='HTML',
                reply_markup=signal_filters_keyboard(user)
            )
    finally:
        db.close()

async def toggle_ma_filter_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ativa ou desativa o filtro de Média Móvel."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(telegram_id=user_id).first()
        if user:
            user.is_ma_filter_enabled = not user.is_ma_filter_enabled
            db.commit()
            await query.edit_message_reply_markup(reply_markup=signal_filters_keyboard(user))
    finally:
        db.close()

async def toggle_rsi_filter_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ativa ou desativa o filtro de RSI."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(telegram_id=user_id).first()
        if user:
            user.is_rsi_filter_enabled = not user.is_rsi_filter_enabled
            db.commit()
            await query.edit_message_reply_markup(reply_markup=signal_filters_keyboard(user))
    finally:
        db.close()

# --- Handlers para configurar os valores (exemplo para Período da MA) ---

async def ask_ma_period(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Pergunta o novo período da Média Móvel."""
    query = update.callback_query
    await query.answer()
    context.user_data['settings_message_id'] = query.message.message_id
    await query.edit_message_text("Envie o período para a Média Móvel (ex: 50).")
    return ASKING_MA_PERIOD

async def receive_ma_period(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe e salva o novo período da MA."""
    user_id = update.effective_user.id
    message_id_to_edit = context.user_data.get('settings_message_id')
    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=update.message.message_id)
    try:
        value = int(update.message.text)
        if not (5 <= value <= 200): raise ValueError("Valor fora do range")

        db = SessionLocal()
        try:
            user = db.query(User).filter_by(telegram_id=user_id).first()
            user.ma_period = value
            db.commit()
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id, message_id=message_id_to_edit,
                text=f"✅ Período da MA atualizado para {value}.",
                reply_markup=signal_filters_keyboard(user)
            )
        finally:
            db.close()
    except (ValueError, TypeError):
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id, message_id=message_id_to_edit,
            text="❌ Valor inválido. Envie um número inteiro entre 5 e 200."
        )
        return ASKING_MA_PERIOD
    return ConversationHandler.END

# --- Handlers para o Timeframe da Média Móvel ---
async def ask_ma_timeframe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exibe as opções de timeframe para o usuário escolher."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(telegram_id=user_id).first()
        if user:
            await query.edit_message_text(
                "Selecione o tempo gráfico para o cálculo da Média Móvel:",
                reply_markup=ma_timeframe_keyboard(user)
            )
    finally:
        db.close()

async def set_ma_timeframe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Define o timeframe escolhido pelo usuário."""
    query = update.callback_query
    await query.answer()
    timeframe = query.data.split('_')[-1]
    user_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(telegram_id=user_id).first()
        if user:
            user.ma_timeframe = timeframe
            user.rsi_timeframe = timeframe # Sincroniza o timeframe do RSI por simplicidade
            db.commit()
            await query.edit_message_text(
                f"✅ Timeframe atualizado para {timeframe} minutos.",
                reply_markup=signal_filters_keyboard(user)
            )
    finally:
        db.close()


# --- Handlers para o Limite de Sobrevenda do RSI ---
async def ask_rsi_oversold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['settings_message_id'] = query.message.message_id
    await query.edit_message_text("Envie o limite de **Sobrevenda** para o RSI (ex: 30).\nSinais de SHORT serão rejeitados se o RSI estiver abaixo deste valor.")
    return ASKING_RSI_OVERSOLD

async def receive_rsi_oversold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    message_id_to_edit = context.user_data.get('settings_message_id')
    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=update.message.message_id)
    try:
        value = int(update.message.text)
        if not (10 <= value <= 40): raise ValueError("Valor fora do range")
        db = SessionLocal()
        try:
            user = db.query(User).filter_by(telegram_id=user_id).first()
            user.rsi_oversold_threshold = value
            db.commit()
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id, message_id=message_id_to_edit,
                text=f"✅ Limite de Sobrevenda do RSI atualizado para {value}.",
                reply_markup=signal_filters_keyboard(user)
            )
        finally:
            db.close()
    except (ValueError, TypeError):
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id, message_id=message_id_to_edit,
            text="❌ Valor inválido. Envie um número inteiro entre 10 e 40."
        )
        return ASKING_RSI_OVERSOLD
    return ConversationHandler.END

# --- Handlers para o Limite de Sobrecompra do RSI ---
async def ask_rsi_overbought(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['settings_message_id'] = query.message.message_id
    await query.edit_message_text("Envie o limite de **Sobrecompra** para o RSI (ex: 70).\nSinais de LONG serão rejeitados se o RSI estiver acima deste valor.")
    return ASKING_RSI_OVERBOUGHT

async def receive_rsi_overbought(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    message_id_to_edit = context.user_data.get('settings_message_id')
    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=update.message.message_id)
    try:
        value = int(update.message.text)
        if not (60 <= value <= 90): raise ValueError("Valor fora do range")
        db = SessionLocal()
        try:
            user = db.query(User).filter_by(telegram_id=user_id).first()
            user.rsi_overbought_threshold = value
            db.commit()
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id, message_id=message_id_to_edit,
                text=f"✅ Limite de Sobrecompra do RSI atualizado para {value}.",
                reply_markup=signal_filters_keyboard(user)
            )
        finally:
            db.close()
    except (ValueError, TypeError):
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id, message_id=message_id_to_edit,
            text="❌ Valor inválido. Envie um número inteiro entre 60 e 90."
        )
        return ASKING_RSI_OVERBOUGHT
    return ConversationHandler.END

async def show_risk_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == query.from_user.id).first()
        if not user:
            await query.edit_message_text("Não encontrei seu usuário. Use /start para registrar.")
            return
        header = ("🧮 <b>Risco & Tamanho</b>\n<i>Ajuste parâmetros de risco e tamanho de posição.</i>\n\n"
                  f"{_risk_summary(user)}")
        await query.edit_message_text(text=header, reply_markup=risk_menu_keyboard(user), parse_mode="HTML")
    except Exception as e:
        logger.error(f"[settings] submenu Risco: {e}", exc_info=True)
        await query.edit_message_text("Não foi possível abrir o submenu de Risco agora.")
    finally:
        db.close()

async def show_stopgain_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == query.from_user.id).first()
        if not user:
            await query.edit_message_text("Não encontrei seu usuário. Use /start para registrar.")
            return
        header = ("🛡️ <b>Stop-Gain</b>\n<i>Configure gatilho e trava do stop-gain.</i>\n\n"
                  f"{_stopgain_summary(user)}")
        await query.edit_message_text(text=header, reply_markup=stopgain_menu_keyboard(user), parse_mode="HTML")
    except Exception as e:
        logger.error(f"[settings] submenu Stop-Gain: {e}", exc_info=True)
        await query.edit_message_text("Não foi possível abrir o submenu de Stop-Gain agora.")
    finally:
        db.close()

async def show_circuit_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except TimedOut:
        # ack do Telegram expirou; segue normalmente
        logger.warning("[settings] circuito: query.answer timeout (ignorado)")
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == query.from_user.id).first()
        if not user:
            await query.edit_message_text("Não encontrei seu usuário. Use /start para registrar.")
            return
        header = ("🚫 <b>Disjuntor</b>\n<i>Defina limite e pausa após disparo.</i>\n\n"
                  f"{_circuit_summary(user)}")
        await query.edit_message_text(text=header, reply_markup=circuit_menu_keyboard(user), parse_mode="HTML")
    except Exception as e:
        logger.error(f"[settings] submenu Disjuntor: {e}", exc_info=True)
        await query.edit_message_text("Não foi possível abrir o submenu de Disjuntor agora.")
    finally:
        db.close()

async def toggle_circuit_scope_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == query.from_user.id).first()
        if not user:
            await query.edit_message_text("Usuário não encontrado. Use /start para registrar.")
            return
        cur = (getattr(user,'circuit_breaker_scope','SIDE') or 'SIDE').upper()
        nxt = 'GLOBAL' if cur == 'SIDE' else ('SYMBOL' if cur == 'GLOBAL' else 'SIDE')
        user.circuit_breaker_scope = nxt
        db.commit()
        header = ("🚫 <b>Disjuntor</b>\n<i>Defina limite e pausa após disparo.</i>\n\n"
                  f"{_circuit_summary(user)}")
        await query.edit_message_text(text=header, reply_markup=circuit_menu_keyboard(user), parse_mode="HTML")
    finally:
        db.close()

async def toggle_reversal_override_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == query.from_user.id).first()
        if not user:
            await query.edit_message_text("Usuário não encontrado. Use /start para registrar.")
            return
        user.reversal_override_enabled = not bool(getattr(user,'reversal_override_enabled', False))
        db.commit()
        header = ("🚫 <b>Disjuntor</b>\n<i>Defina limite e pausa após disparo.</i>\n\n"
                  f"{_circuit_summary(user)}")
        await query.edit_message_text(text=header, reply_markup=circuit_menu_keyboard(user), parse_mode="HTML")
    finally:
        db.close()

async def ask_probe_size(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['settings_message_id'] = query.message.message_id
    await query.edit_message_text("🧪 Envie o <b>tamanho do probe</b> em % (10–100). Ex.: 50", parse_mode='HTML')
    return ASKING_PROBE_SIZE

async def receive_probe_size(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    message_id_to_edit = context.user_data.get('settings_message_id')
    try:
        value = float((update.message.text or '').replace('%','').replace(',','.'))
        if value < 10 or value > 100:
            raise ValueError('range')
        factor = round(value/100.0, 4)
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.telegram_id == user_id).first()
            if not user:
                await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="Usuário não encontrado.")
                return ConversationHandler.END
            user.probe_size_factor = factor
            db.commit()
            header = ("🚫 <b>Disjuntor</b>\n<i>Defina limite e pausa após disparo.</i>\n\n"
                      f"{_circuit_summary(user)}")
            await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text=header, parse_mode='HTML', reply_markup=circuit_menu_keyboard(user))
        finally:
            db.close()
    except Exception:
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="❌ Valor inválido. Envie um inteiro entre 10 e 100.")
        return ASKING_PROBE_SIZE
    return ConversationHandler.END

async def back_to_settings_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except TimedOut:
        logger.warning("[settings] voltar: query.answer timeout (ignorado)")
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == query.from_user.id).first()
        if not user:
            await query.edit_message_text("Não encontrei seu usuário. Use /start para registrar.")
            return
        header = "⚙️ <b>Configurações de Trade</b>\n<i>Escolha uma categoria para ajustar seus parâmetros.</i>"
        try:
            await query.edit_message_text(text=header, reply_markup=settings_menu_keyboard(user), parse_mode="HTML")
        except BadRequest as br:
            # Evita erro barulhento quando o conteúdo é idêntico
            if 'message is not modified' in str(br).lower():
                logger.info("[settings] voltar menu raiz: mensagem não modificada (ignorado)")
                return
            raise
    except Exception as e:
        logger.error(f"[settings] voltar menu raiz: {e}", exc_info=True)
        await query.edit_message_text("Não foi possível voltar ao menu de configurações agora.")
    finally:
        db.close()

async def back_from_whitelist_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sai do estado de edição da Whitelist e volta ao menu de Configurações."""
    # reaproveita o handler existente para renderizar o menu
    await back_to_settings_menu_handler(update, context)
    return ConversationHandler.END

async def show_tp_strategy_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exibe o menu de configuração da estratégia de TP."""
    query = update.callback_query
    await query.answer()
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == query.from_user.id).first()
        if not user:
            await query.edit_message_text("Não encontrei seu usuário.")
            return

        token = (getattr(user, 'tp_distribution', 'EQUAL') or 'EQUAL')
        # Mapeia rótulo e descrição
        def _tp_label_and_info(token: str):
            t = (token or 'EQUAL').upper()
            if t == 'EQUAL':
                return ('Divisão Igual', 'Divide igualmente o volume entre todos os alvos.')
            if t == 'FRONT_HEAVY':
                return ('Mais cedo (frente)', 'Concentra mais fechamento nos primeiros alvos.')
            if t == 'EXP_FRONT':
                return ('Exponencial cedo', 'Concentra fortemente nos primeiros alvos (decay mais agressivo).')
            if t == 'BACK_HEAVY':
                return ('Mais tarde (traseira)', 'Concentra mais fechamento nos últimos alvos.')
            if ',' in (token or ''):
                return ('Personalizada', 'Usa seus valores como âncoras; o bot extrapola e normaliza para os demais alvos.')
            return (token, 'Usa a estratégia configurada.')

        label, info = _tp_label_and_info(token)
        if ',' in (token or ''):
            try:
                parts = [p.strip() for p in token.split(',') if p.strip()]
                fmt = []
                for p in parts:
                    v = float(p)
                    if abs(v - round(v)) < 1e-9:
                        fmt.append(str(int(round(v))))
                    else:
                        s = (f"{v:.2f}").rstrip('0').rstrip('.')
                        fmt.append(s)
                label = f"{label} ({','.join(fmt)})"
            except Exception:
                pass
        header = (
            "🎯 <b>Estratégia de Take Profit</b>\n\n"
            f"<b>Estratégia Atual:</b> {label}\n\n"
            f"{info}"
        )
        await query.edit_message_text(text=header, reply_markup=tp_strategy_menu_keyboard(user), parse_mode='HTML')
    finally:
        db.close()

async def ask_tp_distribution(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Pergunta ao usuário a nova distribuição de TP."""
    query = update.callback_query
    await query.answer()
    
    context.user_data['settings_message_id'] = query.message.message_id
    
    text = (
        "🎯 <b>Personalizar Estratégia de TP</b>\n\n"
        "Envie uma lista de valores (ex.: <code>50,30,20</code>).\n"
        "Esses valores serão usados como <i>âncoras</i>: se houver mais alvos, o bot extrapola a cauda e normaliza para somar 100%.\n"
        "Se quiser voltar ao padrão igualitário, digite <code>igual</code>."
    )
    await query.edit_message_text(text, parse_mode='HTML')
    return ASKING_TP_DISTRIBUTION

async def receive_tp_distribution(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe, valida e salva a nova estratégia de TP."""
    user_id = update.effective_user.id
    message_id_to_edit = context.user_data.get('settings_message_id')
    user_input = (update.message.text or "").strip()

    await update.message.delete()
    
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(telegram_id=user_id).first()
        if not user:
            await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id_to_edit, text="Usuário não encontrado.")
            return ConversationHandler.END

        if user_input.lower() == 'igual':
            user.tp_distribution = 'EQUAL'
            db.commit()
            feedback_text = "✅ Estratégia de TP atualizada para <b>Divisão Igual</b>."
        else:
            try:
                anchors = [float(p.strip()) for p in user_input.replace('%', '').split(',') if p.strip()]
                if not anchors or not all(a > 0 for a in anchors):
                    raise ValueError("Valores precisam ser positivos (ex.: 50,30,20).")
                distribution_str = ",".join(map(str, anchors))
                user.tp_distribution = distribution_str
                db.commit()
                feedback_text = (
                    "✅ Estratégia de TP personalizada salva. Usando seus valores como âncoras; "
                    "normalização e extrapolação serão aplicadas conforme o número de alvos."
                )
            except (ValueError, TypeError) as e:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=message_id_to_edit,
                    text=f"❌ <b>Entrada inválida:</b> {e}\n\nPor favor, tente novamente. Ex.: <code>50,30,20</code> ou <code>igual</code>",
                    parse_mode='HTML'
                )
                return ASKING_TP_DISTRIBUTION # Mantém o usuário na conversa para tentar de novo

        # Se chegou aqui, a entrada foi válida e salva.
        # Reconstrói header com a estratégia atual e descrição
        token = (getattr(user, 'tp_distribution', 'EQUAL') or 'EQUAL')
        def _tp_label_and_info(token: str):
            t = (token or 'EQUAL').upper()
            if t == 'EQUAL':
                return ('Divisão Igual', 'Divide igualmente o volume entre todos os alvos.')
            if t == 'FRONT_HEAVY':
                return ('Mais cedo (frente)', 'Concentra mais fechamento nos primeiros alvos.')
            if t == 'EXP_FRONT':
                return ('Exponencial cedo', 'Concentra fortemente nos primeiros alvos (decay mais agressivo).')
            if t == 'BACK_HEAVY':
                return ('Mais tarde (traseira)', 'Concentra mais fechamento nos últimos alvos.')
            if ',' in (token or ''):
                return ('Personalizada', 'Usa seus valores como âncoras; o bot extrapola e normaliza para os demais alvos.')
            return (token, 'Usa a estratégia configurada.')
        label, info = _tp_label_and_info(token)
        header = (
            "🎯 <b>Estratégia de Take Profit</b>\n\n"
            f"{feedback_text}\n\n"
            f"<b>Estratégia Atual:</b> {label}\n\n{info}"
        )
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=message_id_to_edit,
            text=header,
            reply_markup=tp_strategy_menu_keyboard(user),
            parse_mode='HTML'
        )
    finally:
        db.close()
        
    return ConversationHandler.END

async def cycle_tp_preset_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Alterna entre presets de TP a cada clique: EQUAL → FRONT_HEAVY → BACK_HEAVY → EXP_FRONT → EQUAL."""
    query = update.callback_query
    await query.answer()
    order = ["EQUAL", "FRONT_HEAVY", "BACK_HEAVY", "EXP_FRONT"]
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == query.from_user.id).first()
        if not user:
            await query.edit_message_text("Usuário não encontrado. Use /start.")
            return
        cur = (getattr(user, 'tp_distribution', 'EQUAL') or 'EQUAL').upper()
        try:
            idx = order.index(cur)
        except ValueError:
            idx = 0
        nxt = order[(idx + 1) % len(order)]
        user.tp_distribution = nxt
        db.commit()
        # Reapresenta o menu principal com rótulo e descrição atualizados
        await show_tp_strategy_menu_handler(update, context)
    finally:
        db.close()
