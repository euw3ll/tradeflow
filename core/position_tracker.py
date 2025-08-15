import asyncio
import logging
from telegram.ext import Application
from sqlalchemy.orm import Session
from database.session import SessionLocal
from database.models import Trade, User, PendingSignal
from services.bybit_service import (
    get_market_price, close_partial_position, 
    modify_position_stop_loss, get_order_status
)
from services.notification_service import send_notification # Vamos trocar para notificações individuais
from utils.config import ADMIN_ID
from utils.security import decrypt_data

logger = logging.getLogger(__name__)


async def check_pending_orders_for_user(application: Application, user: User, db: Session):
    """Verifica as ordens limite pendentes de UM usuário específico."""
    pending_orders = db.query(PendingSignal).filter_by(user_telegram_id=user.telegram_id).all()
    if not pending_orders:
        return

    logger.info(f"Rastreador: Verificando {len(pending_orders)} ordem(ns) pendente(s) para o usuário {user.telegram_id}.")
    api_key = decrypt_data(user.api_key_encrypted)
    api_secret = decrypt_data(user.api_secret_encrypted)

    for order in pending_orders:
        status_result = await get_order_status(api_key, api_secret, order.order_id, order.symbol)
        if not status_result.get("success"):
            logger.error(f"Falha ao obter status da ordem {order.order_id} para o usuário {user.telegram_id}.")
            continue

        order_data = status_result["data"]
        order_status = order_data.get("orderStatus")

        if order_status == 'Filled':
            logger.info(f"Ordem Limite {order.order_id} EXECUTADA para o usuário {user.telegram_id}.")
            signal_data = order.signal_data
            new_trade = Trade(
                user_telegram_id=order.user_telegram_id, order_id=order.order_id,
                symbol=signal_data['coin'], side=signal_data['order_type'],
                qty=float(order_data.get('cumExecQty', 0)),
                entry_price=float(order_data.get('avgPrice', signal_data['entries'][0])),
                stop_loss=signal_data['stop_loss'], current_stop_loss=signal_data['stop_loss'],
                initial_targets=signal_data['targets'], status='ACTIVE',
                remaining_qty=float(order_data.get('cumExecQty', 0))
            )
            db.add(new_trade)
            db.delete(order)
            await application.bot.send_message(chat_id=user.telegram_id, text=f"📈 <b>Ordem Limite Executada!</b>\nSua ordem para <b>{order.symbol}</b> foi preenchida.", parse_mode='HTML')

        elif order_status in ['Cancelled', 'Deactivated', 'Rejected']:
            logger.info(f"Ordem Limite {order.order_id} do usuário {user.telegram_id} foi '{order_status}'. Removendo.")
            db.delete(order)
            await application.bot.send_message(chat_id=user.telegram_id, text=f"ℹ️ Sua ordem limite para <b>{order.symbol}</b> foi '{order_status}' pela corretora e removida do monitoramento.", parse_mode='HTML')


async def check_active_trades_for_user(application: Application, user: User, db: Session):
    """Verifica e gerencia os trades ativos de UM usuário específico."""
    active_trades = db.query(Trade).filter(
        Trade.user_telegram_id == user.telegram_id,
        ~Trade.status.like('%CLOSED%')
    ).all()
    if not active_trades:
        return

    logger.info(f"Rastreador: Verificando {len(active_trades)} trade(s) ativo(s) para o usuário {user.telegram_id}.")
    api_key = decrypt_data(user.api_key_encrypted)
    api_secret = decrypt_data(user.api_secret_encrypted)

    for trade in active_trades:
        price_result = await get_market_price(trade.symbol)
        if not price_result.get("success"):
            continue
        
        current_price = price_result["price"]
        
        if trade.initial_targets:
            next_target_price = trade.initial_targets[0]
            if (trade.side == 'LONG' and current_price >= next_target_price) or \
               (trade.side == 'SHORT' and current_price <= next_target_price):
                
                # Esta linha consulta o DB para recalcular o número do TP. É um pouco ineficiente, mas funcional.
                # Poderíamos otimizar no futuro, se necessário.
                db.refresh(trade) # Garante que temos a versão mais recente do trade antes de calcular
                total_initial_targets = len(trade.initial_targets)
                # O número do TP atual é o total de alvos que o sinal TINHA menos o total de alvos restantes + 1.
                # Ex: Tinha 3, restam 2. TP = (len_original - 2 + 1) -> Não temos len_original.
                # Vamos simplificar a notificação por enquanto.
                
                qty_to_close = trade.remaining_qty if len(trade.initial_targets) == 1 else trade.remaining_qty / 2
                close_result = await close_partial_position(api_key, api_secret, trade.symbol, qty_to_close, trade.side)
                
                if close_result.get("success"):
                    new_stop_loss = trade.entry_price if trade.status == 'ACTIVE' else trade.initial_targets[-1]
                    sl_result = await modify_position_stop_loss(api_key, api_secret, trade.symbol, new_stop_loss)

                    if sl_result.get("success"):
                        trade.remaining_qty -= qty_to_close
                        trade.initial_targets = trade.initial_targets[1:]
                        trade.current_stop_loss = new_stop_loss
                        
                        if trade.remaining_qty < 0.0001:
                            trade.status = 'CLOSED_PROFIT'
                        else:
                            # O número do TP é difícil de rastrear sem a contagem original, vamos simplificar
                            trade.status = f'ACTIVE_TP_HIT'
                        
                        # --- NOTIFICAÇÃO CORRIGIDA ---
                        await application.bot.send_message(
                            chat_id=user.telegram_id,
                            text=(
                                f"💰 <b>Take Profit Atingido! ({trade.symbol})</b>\n"
                                f"Posição parcialmente realizada. Novo Stop Loss em ${new_stop_loss:,.4f}."
                            ),
                            parse_mode='HTML'
                        )
                    else:
                        logger.error(f"-> Falha ao mover Stop Loss para {user.telegram_id}: {sl_result.get('error')}")
                        # --- NOTIFICAÇÃO CORRIGIDA ---
                        await application.bot.send_message(chat_id=user.telegram_id, text=f"⚠️ Falha ao mover seu Stop Loss para {trade.symbol}.")
                else:
                    logger.error(f"-> Falha ao fechar posição parcial para {user.telegram_id}: {close_result.get('error')}")
                    # --- NOTIFICAÇÃO CORRIGIDA ---
                    await application.bot.send_message(chat_id=user.telegram_id, text=f"⚠️ Falha ao realizar seu lucro parcial para {trade.symbol}.")

        if (trade.side == 'LONG' and current_price <= trade.current_stop_loss) or \
           (trade.side == 'SHORT' and current_price >= trade.current_stop_loss):
            logger.info(f"STOP LOSS ATINGIDO para {trade.symbol} do usuário {user.telegram_id}.")
            trade.status = 'CLOSED_LOSS'
            await application.bot.send_message(chat_id=user.telegram_id, text=f"🛑 <b>Stop Loss Atingido</b>\n<b>Moeda:</b> {trade.symbol}", parse_mode='HTML')

async def run_tracker(application: Application):
    """Função principal que roda o verificador em loop para TODOS os usuários."""
    logger.info("Iniciando Rastreador de Posições e Ordens (Modo Multiusuário)...")
    while True:
        db = SessionLocal()
        try:
            # 1. Busca todos os usuários que têm chaves de API
            all_users = db.query(User).filter(User.api_key_encrypted.isnot(None)).all()
            if not all_users:
                logger.info("Rastreador: Nenhum usuário com API para verificar.")
            else:
                logger.info(f"Rastreador: Verificando assets para {len(all_users)} usuário(s).")
                # 2. Para cada usuário, roda as verificações
                for user in all_users:
                    await check_pending_orders_for_user(application, user, db)
                    await check_active_trades_for_user(application, user, db)
                
                db.commit() # Salva todas as alterações do loop de uma vez

        except Exception as e:
            logger.critical(f"Erro crítico no loop do rastreador: {e}", exc_info=True)
            db.rollback()
        finally:
            db.close()
        
        await asyncio.sleep(60)