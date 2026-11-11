import logging
import time
from collections import defaultdict
from itertools import islice
from typing import Iterable, Iterator, List, Optional, Tuple

from abstract_customer import (create_abstract_customers, delete_empty_abstract_customers,
                               delete_not_seen, entity_row, filter_abc_customer,
                               get_customer_json, get_lead_json, merge_abstract_customers,
                               upsert)
from amocrm import COMPANIES, CONTACTS, Customer, Lead, amocrm_get
from models import AmoCustomer, AmoLead, db
from sync_index import OwnerIndex

logger = logging.getLogger(__name__)

PAGE_SIZE = 250  # столько же отдаёт amoCRM за один запрос


def pages(items: Iterable, size: int) -> Iterator[list]:
    items = iter(items)
    while page := list(islice(items, size)):
        yield page


def entity_ids(items) -> set:
    return {item["id"] for item in items or [] if item.get("id") is not None}


def customer_action(customer: Customer, index: OwnerIndex, run_id: int) -> Optional[dict]:
    """Строка покупателя с найденным клиентом
    None - если покупателя пропускаем."""
    row = entity_row(get_customer_json(customer), run_id)
    if not row["login"]:
        return None  # покупатель без логина пропускается
    row["abstract_customer_id"] = index.find_customer(row["id"], row["login"])
    return row


def lead_action(lead: Lead, index: OwnerIndex, run_id: int,
                merges: List[Tuple[int, int]]) -> dict:
    """Строка сделки с найденным клиентом
    слияние только отмечается в merges."""
    row = entity_row(get_lead_json(lead), run_id)
    by_id, by_login = index.find_lead(row["id"], row["login"])
    if by_id is not None and by_login is not None and by_id != by_login:
        # две разные записи: сливаются в найденную по сделке, вторая удаляется
        index.merge(by_id, by_login)
        merges.append((by_id, by_login))
    row["abstract_customer_id"] = by_id if by_id is not None else by_login
    return row


def save_page(model, rows: List[dict], index: OwnerIndex,
              merges: List[Tuple[int, int]] = ()) -> None:
    """Записать страницу: новые клиенты, слияния, upsert сущностей."""
    rows = list({row["id"]: row for row in rows}.values())  # повтор id на странице — берём последний

    orphans = [row for row in rows if row["abstract_customer_id"] is None]
    for row, new_id in zip(orphans, create_abstract_customers(len(orphans))):
        row["abstract_customer_id"] = new_id  # не нашлось ничего — новая запись

    for keep_id, drop_id in merges:
        merge_abstract_customers(keep_id, drop_id)

    save = index.save_customer if model is AmoCustomer else index.save_lead
    for row in rows:
        # клиент мог быть слит в другого ниже по странице
        row["abstract_customer_id"] = index.resolve(row["abstract_customer_id"])
        save(row["id"], row["abstract_customer_id"], row["login"])
    upsert(model, rows)


def collect_leads_customers(is_full=True):
    """
    Берём всех покупателей, затем все сделки, и привязываем их к AbstractCustomer. 
    Сопоставление идёт по индексу в памяти (sync_index.py),
    запись — пачкой на страницу CRM с одним commit.
    """
    session = db.session
    run_id = time.time_ns()  # метка прогона для last_seen_run
    index = OwnerIndex.load(session)
    has_errors = False
    # контакты и компании из выгрузки по клиентам — для чистки
    ids_map = defaultdict(lambda: {"contacts": set(), "companies": set()})

    def remember_ids(entity, row):
        ids = ids_map[row["abstract_customer_id"]]
        ids["contacts"] |= entity_ids(entity.contacts)
        ids["companies"] |= entity_ids(entity.companies)

    try:
        customers: Iterator[Customer] = amocrm_get(Customer, _with=f"{COMPANIES},{CONTACTS}")
        for page in pages(customers, PAGE_SIZE):
            saved = []
            for customer in page:
                if is_full is False and customer.status_id == 111349:
                    continue
                try:
                    row = customer_action(customer, index, run_id)
                except Exception:
                    has_errors = True
                    logger.exception(f"Покупатель пропущен: {customer}")
                    continue
                if row is not None:
                    saved.append((customer, row))
            save_page(AmoCustomer, [row for _, row in saved], index)
            session.commit()
            for customer, row in saved:
                remember_ids(customer, row)
        logger.info(f"Collected customers {is_full=}")

        amo_leads: Iterator[Lead] = amocrm_get(Lead, _with=f"{CONTACTS}")
        for page in pages(amo_leads, PAGE_SIZE):
            saved, merges = [], []
            for amo_lead in page:
                if is_full is False and amo_lead.status_id == 143:
                    continue
                try:
                    row = lead_action(amo_lead, index, run_id, merges)
                except Exception:
                    has_errors = True
                    logger.exception(f"Сделка пропущена: {amo_lead}")
                    continue
                saved.append((amo_lead, row))
            save_page(AmoLead, [row for _, row in saved], index, merges)
            session.commit()
            for amo_lead, row in saved:
                remember_ids(amo_lead, row)
        logger.info(f"Collected leads {is_full=}")
    except Exception:
        session.rollback()
        logger.exception("Синхронизация прервана, чистка не запускается")
        raise

    if is_full is False:
        return
    if has_errors:
        logger.error("Часть сущностей не обработана, чистка не запускается")
        return

    cleanup(run_id, ids_map, index)


def cleanup(run_id: int, ids_map: dict, index: OwnerIndex) -> None:
    """Удалить то, чего не было в выгрузке. Одна транзакция: всё или ничего."""
    session = db.session
    try:
        logger.info("Чистка сущностей, которых не было в выгрузке")
        customers = delete_not_seen(AmoCustomer, run_id)
        leads = delete_not_seen(AmoLead, run_id)

        logger.info("Чистка покупателей общая")
        abc_customers = delete_empty_abstract_customers()

        logger.info("Чистка покупателей на уровне сущностей")
        # контакты собирались под id клиентов, которые потом могли слиться
        seen = defaultdict(lambda: {"contacts": set(), "companies": set()})
        for abc_id, ids in ids_map.items():
            target = seen[index.resolve(abc_id)]
            target["contacts"] |= ids["contacts"]
            target["companies"] |= ids["companies"]
        changed = filter_abc_customer(seen)

        session.commit()
    except Exception:
        session.rollback()
        logger.exception("Чистка не выполнена, изменения отменены")
        raise
    logger.info(f"Чистка: удалено покупателей {customers}, сделок {leads}, клиентов {abc_customers}; "
                f"контакты/компании изменены у {changed}")
