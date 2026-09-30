"""Explicit opt-in reminders for the verified 2026 calendar, checked 2026-09-30.

Two verified 2027 dates are included: the USN declaration (2027-04-26) and
additional contributions (2027-07-01), both for 2026. Patent terms, VAT and
employee reporting are not inferred. NPD/AUSN reminders ask to check assessed
tax; a selected regime and an IP registration date do not establish tax debts.
Registration notifications use the federal 2026 non-working-day calendar; local
holidays and individual extensions need a separate check with the tax authority.
"""

from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


AUTO_PREFIX = 'auto:tax-calendar:2026v1:'
USN = {'usn_income', 'usn_profit'}
AUSN = {'ausn_income', 'ausn_profit'}
CONTRIBUTIONS = USN | {'psn', 'osno'}
REGIMES = CONTRIBUTIONS | AUSN | {'npd'}
SOURCES = {
    'usn_notice': 'https://www.nalog.gov.ru/rn25/ifns/r25_02/info/16625480/',
    'ausn_notice': 'https://ausn.nalog.gov.ru/',
    'fixed': 'https://www.nalog.gov.ru/rn46/news/activities_fts/16603931/',
    'additional': 'https://www.nalog.gov.ru/rn16/news/tax_doc_news/16617063/',
    'usn_advance': 'https://www.nalog.gov.ru/rn91/ifns/ifns_simph/info/16608587/',
    'usn_declaration': 'https://www.nalog.gov.ru/rn19/news/activities_fts/16641463/',
    'npd_monthly': 'https://npd.nalog.ru/faq/',
    'ausn_monthly': 'https://ausn.nalog.gov.ru/',
    'weekend_transfers': 'https://government.ru/docs/all/161028/',
    'holidays': 'https://mintrud.gov.ru/social/social/1366',
}

# Weekends are handled separately. The two government transfers are Jan 9/Dec 31.
HOLIDAYS_2026 = {date(2026, 1, day) for day in range(1, 10)} | {
    date(2026, 2, 23), date(2026, 3, 8), date(2026, 3, 9),
    date(2026, 5, 1), date(2026, 5, 9), date(2026, 5, 11),
    date(2026, 6, 12), date(2026, 11, 4), date(2026, 12, 31),
}
CALENDAR_NOTE = (
    'Календарь ограничен регистрацией и сроками 2026 года; из 2027 года подтверждён '
    'срок декларации УСН ИП за 2026 год — 26.04.2027 и дополнительного взноса за 2026 год — 01.07.2027. '
    'Декларации по другим налогам, НДС, отчётность за работников и сроки патента '
    'не включены. НПД/АУСН за декабрь 2026 с уплатой в январе 2027 не включены. '
    'Проверьте эти сроки и обязанности после смены режима отдельно в ФНС.'
)


def _working_day_2026(day):
    while day.year == 2026 and (day.weekday() >= 5 or day in HOLIDAYS_2026):
        day += timedelta(days=1)
    return day if day.year == 2026 else None


def _plan(state, now):
    """Build future reminders without modifying state or doing I/O."""
    result = {'notes': [CALENDAR_NOTE], 'deadlines': [], 'desired': {}, 'valid_keys': set()}
    if not isinstance(state, dict) or state.get('reminders_enabled') is not True:
        result['notes'].append('Автоматические напоминания отключены; требуется явное согласие.')
        return result
    registration = state.get('registration')
    raw = registration.get('date') if isinstance(registration, dict) else None
    try:
        if not isinstance(raw, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw):
            raise ValueError
        registered = date.fromisoformat(raw)
    except ValueError:
        result['notes'].append('Нужна дата регистрации в формате ГГГГ-ММ-ДД.')
        return result
    zone_name = state.get('timezone', 'Europe/Moscow')
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        result['notes'].append('Неизвестный часовой пояс; укажите IANA-зону, например Europe/Moscow.')
        return result
    if registered.year != 2026 or registered > now.astimezone(zone).date():
        result['notes'].append('Автоматический календарь доступен только для уже зарегистрированного в 2026 году ИП.')
        return result
    regime = state.get('selected_regime')
    if not isinstance(regime, str) or regime not in REGIMES:
        result['notes'].append('Сначала выберите поддерживаемый налоговый режим.')
        return result

    def add(ident, deadline, label, source):
        result['deadlines'].append({'id': ident, 'date': deadline.isoformat(), 'label': label, 'source': source})

    if regime in USN | AUSN:
        limit = _working_day_2026(registered + timedelta(days=30))
        if limit is None:
            result['notes'].append('30-дневный срок уведомления выходит в 2027 год; переносы не подтверждены, уточните дату в ФНС.')
        else:
            name, source = ('УСН', SOURCES['usn_notice']) if regime in USN else ('АУСН', SOURCES['ausn_notice'])
            add('registration_notice', limit,
                f'Уведомление о применении {name}: 30 календарных дней после постановки на учёт. '
                'Если уже подали, повторная подача не нужна.', source)
            result['notes'].append('Срок уведомления рассчитан от указанной даты регистрации с федеральными переносами 2026 года; региональные праздники и специальные продления проверьте в ФНС.')
    if regime in CONTRIBUTIONS:
        # No claim that every user owes the full-year amount: exclusions and partial
        # years are intentionally described as a check, without calculating payment.
        if registered <= date(2026, 12, 28):
            add('fixed_2026', date(2026, 12, 28),
                'Проверьте уплату фиксированных взносов ИП за 2026 год. '
                'Сумма зависит от периода регистрации и возможных освобождений.', SOURCES['fixed'])
        else:
            result['notes'].append('Регистрация после 28.12.2026: срок и сумму фиксированных взносов уточните в ФНС.')
        add('additional_2026', date(2027, 7, 1),
            'Проверьте дополнительный страховой взнос 1% за 2026 год: он возникает, '
            'если база по вашему режиму превышает 300 000 ₽; учитываются освобождения.', SOURCES['additional'])
    if regime in USN:
        add('usn_declaration_2026', date(2027, 4, 26),
            'Представьте декларацию ИП по УСН за 2026 год, если применяли УСН в этом году. '
            'Указан общий годовой срок; при прекращении деятельности сроки могут отличаться.',
            SOURCES['usn_declaration'])
        for period_end, deadline, ident, period in (
            (date(2026, 3, 31), date(2026, 4, 28), 'usn_q1', 'I квартал'),
            (date(2026, 6, 30), date(2026, 7, 28), 'usn_h1', 'полугодие'),
            (date(2026, 9, 30), date(2026, 10, 28), 'usn_q3', '9 месяцев'),
        ):
            if registered <= period_end:
                add(ident, deadline,
                    f'Проверьте и уплатите аванс УСН за {period} 2026 года, если есть сумма к уплате. '
                    'Уведомление об исчисленной сумме подаётся раньше; его срок проверьте отдельно.', SOURCES['usn_advance'])
    if regime in AUSN | {'npd'}:
        result['notes'].append('Фиксированные взносы ИП за себя для выбранного режима не назначаются.')
        result['notes'].append('Ежемесячные напоминания — проверка начислений, а не установленная обязанность уплаты. '
                               'Дата начала режима может отличаться от даты регистрации ИП; ориентируйтесь на уведомление ФНС.')
        if regime == 'npd':
            result['notes'].append('Первый налоговый период НПД длится до конца следующего месяца после постановки на учёт НПД. '
                                   'В первую ежемесячную проверку начисления может ещё не быть.')
        name, payment_day, source, cabinet = (
            ('npd', 28, SOURCES['npd_monthly'], '«Мой налог»') if regime == 'npd'
            else ('ausn', 25, SOURCES['ausn_monthly'], 'личном кабинете АУСН')
        )
        # Check only months starting with IP registration, through November 2026.
        # No assumption that the user has taxable revenue or an assessed amount.
        for month in range(registered.month, 12):
            deadline = _working_day_2026(date(2026, month + 1, payment_day))
            add(f'{name}_check_2026_{month:02}', deadline,
                f'Проверьте начисление в {cabinet} после месяца {month:02}.2026. '
                'Если ФНС начислила налог к этой дате — уплатите указанную сумму; '
                'при отсутствии начисления платёж не требуется.', source)

    fingerprint = hashlib.sha256(json.dumps([raw, regime, zone.key], ensure_ascii=False).encode()).hexdigest()[:20]
    for deadline in result['deadlines']:
        last_day = date.fromisoformat(deadline['date'])
        for before in (3, 0):
            local = datetime.combine(last_day - timedelta(days=before), time(9), tzinfo=zone)
            due_at = local.astimezone(timezone.utc)
            key = f'{AUTO_PREFIX}{fingerprint}:{deadline["id"]}:{before}'
            # A queued reminder may already be due while the one worker is busy.
            # Repeating the same profile must not cancel that pending delivery.
            result['valid_keys'].add(key)
            if due_at <= now:
                continue
            timing = 'Через 3 дня' if before else 'Сегодня'
            text = (f'{timing}, {last_day:%d.%m.%Y}, срок: {deadline["label"]}\n'
                    f'Источник ФНС: {deadline["source"]}')
            result['desired'][key] = {'text': text, 'due_at': due_at}
    return result


def sync_reminders(services, user_id, state, *, now=None):
    """Reconcile automatic reminders, preserving manual ones and sent records.

    Returns counts and calendar coverage notes. Uses one PostgreSQL transaction;
    Services/Store own the connection. Repeated calls neither duplicate delivery
    nor reactivate an explicitly cancelled reminder. Only a previous automatic
    cancellation caused by profile changes can be restored after a new opt-in.
    """
    now = datetime.now(timezone.utc) if now is None else now
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('now должен содержать часовой пояс.')
    plan = _plan(state, now)
    result = {'created': 0, 'cancelled': 0, 'notes': plan['notes'], 'deadlines': plan['deadlines']}
    with services.db.transaction():
        services.db.execute('SELECT pg_advisory_xact_lock(1296128066, 5)')
        existing = {row['idempotency_key']: row for row in services.db.execute('''
            SELECT r.id, r.idempotency_key, r.status, o.last_error
            FROM service_reminders r LEFT JOIN service_outbox o ON o.reminder_id = r.id
            WHERE r.user_id = %s AND r.idempotency_key LIKE %s''', (user_id, AUTO_PREFIX + '%'))}
        for key, row in existing.items():
            if row['status'] == 'pending' and key not in plan['valid_keys']:
                if services.cancel_reminder(user_id, row['id']):
                    result['cancelled'] += 1
                    services.db.execute('''UPDATE service_outbox SET last_error = 'auto_profile_changed'
                        WHERE reminder_id = %s AND NOT delivered''', (row['id'],))
        for key, item in plan['desired'].items():
            row = existing.get(key)
            if row is None:
                services.create_reminder(user_id, item['text'], item['due_at'], key)
                result['created'] += 1
            elif row['status'] == 'cancelled' and row['last_error'] == 'auto_profile_changed':
                services.db.execute("UPDATE service_reminders SET status = 'pending' WHERE id = %s", (row['id'],))
                services.db.execute('''UPDATE service_outbox SET cancelled = FALSE, last_error = NULL,
                    dead_letter = FALSE, attempts = 0, next_attempt_at = due_at
                    WHERE reminder_id = %s AND NOT delivered''', (row['id'],))
                result['created'] += 1
    return result
