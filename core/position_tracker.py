import asyncio
import logging
from telegram.ext import Application
from database.session import SessionLocal
from database.models import Trade, User, PendingSignal
from services.bybit_service import get_market_price, close_partial_position, modify_position_stop_loss, get_order_status
from services.notification_service import send_notification
from utils.security import decrypt_data
from utils.config import ADMIN_ID

logger = logging.getLogger(__name__)

async def check_active_trades(application: Application):
    """Verifica e gerencia ativamente os trades com múltiplos TPs e trailing stop."""
    db = SessionLocal()
    try:
        # Busca todos os trades que não estão completamente fechados
        active_trades = db.query(Trade).filter(~Trade.status.like('%CLOSED%')).all()
        if not active_trades: return

        logger.info(f"Rastreador: {len(active_trades)} trade(s) ativo(s) para verificar.")
        
        admin_user = db.query(User).filter_by(telegram_id=ADMIN_ID).first()
        if not admin_user or not admin_user.api_key_encrypted:
            logger.error("Rastreador: Admin ou chaves de API não encontrados.")
            return
        
        api_key = decrypt_data(admin_user.api_key_encrypted)
        api_secret = decrypt_data(admin_user.api_secret_encrypted)

        for trade in active_trades:
            price_result = get_market_price(trade.symbol)
            if not price_result.get("success"):
                # ... (código de erro)
                continue
            
            current_price = price_result["price"]
            
            # --- LÓGICA DE TAKE PROFIT (MULTI-ALVO) ---
            if trade.initial_targets:
                next_target_price = trade.initial_targets[0]

                if (trade.side == 'LONG' and current_price >= next_target_price) or \
                   (trade.side == 'SHORT' and current_price <= next_target_price):
                    
                    total_targets = len(db.query(Trade).filter_by(id=trade.id).first().initial_targets)
                    current_tp_number = total_targets - len(trade.initial_targets) + 1
                    
                    logger.info(f"✅ TP{current_tp_number} ATINGIDO para {trade.symbol}! Preço: {current_price}")
                    
                    # Define a quantidade a ser fechada (ex: 50% no TP1, 100% do restante no TP2)
                    qty_to_close = trade.remaining_qty if len(trade.initial_targets) == 1 else trade.remaining_qty / 2
                    
                    close_result = close_partial_position(api_key, api_secret, trade.symbol, qty_to_close, trade.side)
                    
                    if close_result.get("success"):
                        # --- LÓGICA DE TRAILING STOP ---
                        # No TP1, move para o breakeven. Nos TPs seguintes, move para o TP anterior.
                        new_stop_loss = trade.entry_price if trade.status == 'ACTIVE' else trade.initial_targets[-1]
                        
                        sl_result = modify_position_stop_loss(api_key, api_secret, trade.symbol, new_stop_loss)

                        if sl_result.get("success"):
                            # Atualiza o trade no banco de dados
                            trade.remaining_qty -= qty_to_close
                            trade.initial_targets = trade.initial_targets[1:]
                            trade.current_stop_loss = new_stop_loss # Atualiza o SL no DB
                            
                            if trade.remaining_qty < 0.0001:
                                trade.status = 'CLOSED_PROFIT'
                            else:
                                trade.status = f'ACTIVE_TP{current_tp_number}'
                            
                            await send_notification(
                                application,
                                f"💰 <b>TP{current_tp_number} Atingido! ({trade.symbol})</b>\n"
                                f"Posição parcialmente realizada. Novo Stop Loss em ${new_stop_loss:,.4f}."
                            )
                        else:
                            logger.error(f"-> Falha ao mover Stop Loss: {sl_result.get('error')}")
                            await send_notification(application, f"⚠️ Falha ao mover Stop Loss para {trade.symbol}.")
                    else:
                        logger.error(f"-> Falha ao fechar posição parcial: {close_result.get('error')}")
                        await send_notification(application, f"⚠️ Falha ao realizar lucro parcial para {trade.symbol}.")

            # --- LÓGICA DE STOP LOSS ---
            if (trade.side == 'LONG' and current_price <= trade.current_stop_loss) or \
               (trade.side == 'SHORT' and current_price >= trade.current_stop_loss):
                logger.info(f"❌ STOP LOSS ATINGIDO para {trade.symbol}! Preço: {current_price}, Stop: {trade.current_stop_loss}")
                trade.status = 'CLOSED_LOSS'
                await send_notification(
                    application,
                    f"🛑 <b>Stop Loss Atingido</b>\n<b>Moeda:</b> {trade.symbol}\nPosição foi fechada pela corretora."
                )
        
        db.commit()
        
    except Exception as e:
        logger.error(f"Erro no ciclo do rastreador de posições: {e}", exc_info=True)
    finally:
        db.close()

async def run_tracker(application: Application):
    """Função principal que roda o verificador em loop."""
    logger.info("Iniciando Rastreador de Posições e Ordens...")
    while True:
        try:
            # --- LINHA ADICIONADA ---
            await check_pending_orders(application) # Verifica ordens limite primeiro
            
            await check_active_trades(application) # Depois verifica posições ativas
        except Exception as e:
            logger.critical(f"Erro crítico no loop do rastreador: {e}", exc_info=True)
        
        await asyncio.sleep(30) # Reduzindo o tempo para 30s para uma verificação mais rápida

async def check_pending_orders(application: Application):
    """Verifica ordens limite pendentes para ver se foram executadas."""
    db = SessionLocal()
    try:
        pending_orders = db.query(PendingSignal).all()
        if not pending_orders:
            return

        logger.info(f"Rastreador de Ordens: {len(pending_orders)} ordem(ns) limite pendente(s) para verificar.")
        
        admin_user = db.query(User).filter_by(telegram_id=ADMIN_ID).first()
        if not admin_user or not admin_user.api_key_encrypted:
            logger.error("Rastreador de Ordens: Admin ou chaves de API não encontrados.")
            return
        
        api_key = decrypt_data(admin_user.api_key_encrypted)
        api_secret = decrypt_data(admin_user.api_secret_encrypted)

        for order in pending_orders:
            status_result = await get_order_status(api_key, api_secret, order.order_id, order.symbol)
            
            if not status_result.get("success"):
                logger.error(f"Não foi possível obter o status da ordem {order.order_id} para {order.symbol}.")
                continue

            order_data = status_result["data"]
            order_status = order_data.get("orderStatus")
            
            if order_status == 'Filled':
                logger.info(f"✅ ORDEM LIMITE EXECUTADA: {order.symbol} (ID: {order.order_id}). Convertendo para um trade ativo.")
                
                signal_data = order.signal_data
                
                # Cria o novo trade na tabela de trades ativos
                new_trade = Trade(
                    user_telegram_id=order.user_telegram_id,
                    order_id=order.order_id,
                    symbol=signal_data['coin'],
                    side=signal_data['order_type'],
                    qty=float(order_data.get('cumExecQty', 0)),
                    entry_price=float(order_data.get('avgPrice', signal_data['entries'][0])),
                    stop_loss=signal_data['stop_loss'],
                    current_stop_loss=signal_data['stop_loss'],
                    initial_targets=signal_data['targets'],
                    status='ACTIVE',
                    remaining_qty=float(order_data.get('cumExecQty', 0))
                )
                db.add(new_trade)
                db.delete(order) # Remove da lista de ordens pendentes
                
                await send_notification(
                    application,
                    f"📈 <b>Ordem Limite Executada!</b>\n"
                    f"Sua ordem para <b>{order.symbol}</b> foi preenchida.\n"
                    f"A posição agora está sendo gerenciada ativamente."
                )

            elif order_status in ['Cancelled', 'Deactivated', 'Rejected']:
                logger.info(f"Ordem Limite {order.order_id} para {order.symbol} foi '{order_status}'. Removendo do monitoramento.")
                db.delete(order)
                await send_notification(
                    application,
                    f"ℹ️ Sua ordem limite para <b>{order.symbol}</b> foi '{order_status}' e removida do monitoramento."
                )
        
        db.commit()

    except Exception as e:
        logger.error(f"Erro no ciclo do rastreador de ordens pendentes: {e}", exc_info=True)
        db.rollback()
    finally:
        db.close()