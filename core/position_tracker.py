import asyncio
import logging
from telegram.ext import Application
from database.session import SessionLocal
from database.models import Trade, User
from services.bybit_service import get_market_price, close_partial_position, modify_position_stop_loss
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
    logger.info("Iniciando Rastreador de Posições...")
    while True:
        try:
            await check_active_trades(application)
        except Exception as e:
            logger.critical(f"Erro crítico no loop do rastreador: {e}", exc_info=True)
        # Espera 60 segundos antes de verificar novamente
        await asyncio.sleep(60)