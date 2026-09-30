from copy import deepcopy
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import unittest

from ip_bot.tax import compare_taxes, render_comparison, render_tax_details


TODAY = date(2026, 9, 30)
BASE = {
    "activity": "Разработка сайтов", "region": "Самарская область",
    "revenue": "1200000", "expenses": "300000", "employees": 0, "clients": "Физлица",
}


def comparison(**updates):
    return compare_taxes({**BASE, **updates}, today=TODAY)


def row(result, name):
    return next(item for item in result["rows"] if item["id"] == name)


class TaxTests(unittest.TestCase):
    def test_inputs_are_not_changed_and_output_is_serializable(self):
        answers = deepcopy(BASE)
        result = compare_taxes(answers, today=TODAY)
        self.assertEqual(answers, BASE)
        self.assertEqual(len(result["rows"]), 7)
        json.dumps(result, ensure_ascii=False, allow_nan=False)

    def test_rules_are_versioned_and_every_rule_has_official_sources(self):
        rules = json.loads((Path(__file__).resolve().parents[1] / "config/tax_rules.json").read_text())
        self.assertEqual(rules["verified_on"], "2026-09-30")
        self.assertTrue(rules["version"])
        for rule in rules["rules"].values():
            self.assertTrue(rule["sources"])
            self.assertTrue(all(url.startswith(("https://www.nalog.gov.ru/", "https://npd.nalog.ru/", "https://ausn.nalog.gov.ru/")) for url in rule["sources"]))

    def test_historical_and_future_years_never_reuse_2026_rules(self):
        for year in (2025, 2027, "2027"):
            with self.subTest(year=year):
                result = comparison(year=year)
                self.assertIsNone(result["recommendation"])
                self.assertTrue(all(item["status"] == "needs_input" for item in result["rows"]))
                self.assertTrue(all("total" not in item and "range" not in item for item in result["rows"]))
                self.assertIn("Расчёт не выполнен", render_comparison(result))
        result = compare_taxes(BASE, today=date(2027, 1, 1))
        self.assertEqual(result["year"], 2027)
        self.assertIsNone(result["recommendation"])

    def test_invalid_numbers_are_not_silently_zeroed(self):
        for key, value in (("revenue", "NaN"), ("expenses", "-1"), ("payroll", "Infinity"),
                           ("employees", "0.5"), ("employees", True), ("client_person_share", "50"),
                           ("usn_income_rate", "7"), ("usn_profit_rate", "1"),
                           ("npd_deduction_remaining", "10001"), ("year", "2026.0")):
            with self.subTest(key=key, value=value):
                result = comparison(**{key: value})
                self.assertTrue(all(item["status"] == "needs_input" for item in result["rows"]))
                self.assertTrue(all("total" not in item for item in result["rows"]))
                self.assertIn(key, result["notes"][0])

    def test_npd_uses_client_rates_and_never_adds_fixed_contributions(self):
        for clients, expected in (("Физлица", "48000.00"), ("ИП и организации", "72000.00")):
            with self.subTest(clients=clients):
                item = row(comparison(clients=clients, npd_deduction_remaining=0), "npd")
                self.assertEqual(item["total"], expected)
                self.assertEqual(item["components"]["own_contributions"], "0.00")
                self.assertEqual(item["status"], "conditional")

    def test_npd_unknown_split_and_bonus_are_ranges_not_average_or_free_bonus(self):
        item = row(comparison(clients="Физлица, ИП и организации"), "npd")
        self.assertNotIn("total", item)
        self.assertEqual(item["range"]["min"], "38000.00")
        self.assertEqual(item["range"]["max"], "72000.00")
        self.assertEqual(set(item["missing"]), {"client_person_share", "npd_deduction_remaining", "npd_activity_eligible"})
        item = row(comparison(clients="Физлица, ИП и организации", client_person_share="0.25", npd_deduction_remaining="2000"), "npd")
        self.assertEqual(item["total"], "64000.00")

    def test_npd_deduction_is_limited_by_reduced_rates(self):
        item = row(comparison(revenue="10000", npd_deduction_remaining="10000"), "npd")
        self.assertEqual(item["total"], "300.00")

    def test_decimal_money_is_not_computed_with_binary_floats(self):
        item = row(comparison(revenue="1000,25", npd_deduction_remaining=0), "npd")
        self.assertEqual(item["total"], "40.01")

    def test_total_equals_rounded_components_even_at_fractional_kopecks(self):
        result = comparison(revenue="1234567.89", expenses="300000.11", new_ip=True,
                            usn_income_rate=6, usn_profit_rate=15, expenses_confirmed=True,
                            expenses_include_contributions=True, vat_amount="100.23",
                            other_income=0, tax_resident=True)
        for item in result["rows"]:
            if "total" in item:
                self.assertEqual(Decimal(item["total"]), sum(Decimal(value) for value in item["components"].values()))

    def test_income_and_employee_limits_use_strict_exceeding(self):
        cases = (("npd", "2400000", 0), ("patent", "20000000", 15),
                 ("ausn_income", "60000000", 5), ("usn_income", "490500000", 130))
        for name, limit, staff in cases:
            with self.subTest(name=name):
                self.assertNotEqual(row(comparison(revenue=limit, employees=staff), name)["status"], "excluded")
                self.assertEqual(row(comparison(revenue=str(Decimal(limit) + Decimal("0.01")), employees=staff), name)["status"], "excluded")
                self.assertEqual(row(comparison(revenue=limit, employees=staff + 1), name)["status"], "excluded")

    def test_npd_with_one_employee_is_excluded_without_calculation(self):
        item = row(comparison(employees=1), "npd")
        self.assertEqual(item["status"], "excluded")
        self.assertNotIn("total", item)

    def test_missing_expenses_and_staff_are_not_zero(self):
        result = comparison(expenses=None)
        for name in ("usn_profit", "ausn_profit", "osno"):
            self.assertIn("expenses", row(result, name)["missing"])
            self.assertNotIn("total", row(result, name))
        result = comparison(employees=None)
        self.assertTrue(all("total" not in item for item in result["rows"]))

    def test_usn_income_reduces_tax_by_contributions_without_double_charge(self):
        item = row(comparison(previous_revenue="0", usn_income_rate="6"), "usn_income")
        self.assertEqual(item["components"]["own_contributions"], "66390.00")
        self.assertEqual(item["components"]["tax"], "5610.00")
        self.assertEqual(item["total"], "72000.00")
        low = row(comparison(revenue="100000", new_ip=True, usn_income_rate="6"), "usn_income")
        self.assertEqual(low["total"], "57390.00")
        self.assertEqual(low["components"]["tax"], "0.00")

    def test_usn_employee_deduction_is_capped_at_half(self):
        item = row(comparison(employees=1, payroll="500000", employer_contributions="150000",
                              previous_revenue=0, usn_income_rate=6), "usn_income")
        self.assertEqual(item["components"]["tax"], "36000.00")
        self.assertEqual(item["total"], "252390.00")

    def test_own_additional_contribution_cap_is_2026_cap(self):
        item = row(comparison(revenue="100000000", vat_amount=0, usn_income_rate=6), "usn_income")
        self.assertEqual(item["components"]["own_contributions"], "379208.00")

    def test_vat_is_not_zeroed_from_current_revenue_alone(self):
        item = row(comparison(usn_income_rate=6), "usn_income")
        self.assertNotIn("total", item)
        self.assertIn("vat_amount", item["missing"])
        self.assertIn("previous_revenue", item["missing"])
        self.assertIn("subtotal", item)
        item = row(comparison(revenue="20000000", previous_revenue="20000000", usn_income_rate=6), "usn_income")
        self.assertEqual(item["components"]["vat"], "0.00")
        above = row(comparison(revenue="20000000.01", previous_revenue=0, usn_income_rate=6), "usn_income")
        self.assertNotIn("total", above)
        prior_above = row(comparison(previous_revenue="20000000.01", usn_income_rate=6), "usn_income")
        self.assertNotIn("total", prior_above)

    def test_unknown_regional_rate_never_claims_verified_availability(self):
        item = row(comparison(new_ip=True), "usn_income")
        self.assertEqual(item["total"], "72000.00")
        self.assertIn("usn_income_rate", item["missing"])
        self.assertFalse(item["comparable"])

    def test_usn_profit_minimum_tax_and_contributions_use_profit(self):
        item = row(comparison(expenses="1190000", new_ip=True, usn_profit_rate=15,
                              expenses_confirmed=True, expenses_include_contributions=True), "usn_profit")
        self.assertEqual(item["components"]["tax"], "12000.00")
        self.assertEqual(item["components"]["own_contributions"], "57390.00")
        self.assertEqual(item["total"], "69390.00")
        regular = row(comparison(new_ip=True, usn_profit_rate=15, expenses_confirmed=True,
                                 expenses_include_contributions=True), "usn_profit")
        self.assertEqual(regular["components"]["tax"], "135000.00")
        self.assertEqual(regular["components"]["own_contributions"], "63390.00")
        self.assertEqual(regular["total"], "198390.00")

    def test_uncertain_expense_composition_produces_range_not_double_deduction(self):
        item = row(comparison(new_ip=True, usn_profit_rate=15), "usn_profit")
        self.assertNotIn("total", item)
        self.assertIn("range", item)
        self.assertIn("expenses_include_contributions", item["missing"])
        self.assertIn("expenses_confirmed", item["missing"])

    def test_payroll_is_not_flat_multiplied_or_silently_ignored(self):
        item = row(comparison(employees=2, payroll="6000000", new_ip=True, usn_income_rate=6), "usn_income")
        self.assertIn("employer_contributions", item["missing"])
        self.assertNotIn("total", item)
        self.assertFalse(item["comparable"])

    def test_patent_previous_year_limit_and_potential_income(self):
        excluded = row(comparison(previous_revenue="21000000", patent_cost=60000, patent_potential_income=1000000), "patent")
        self.assertEqual(excluded["status"], "excluded")
        item = row(comparison(revenue="10000000", previous_revenue=0, patent_cost=60000, patent_potential_income=1000000), "patent")
        self.assertEqual(item["components"]["own_contributions"], "64390.00")
        self.assertEqual(item["total"], "64390.00")
        unknown = row(comparison(patent_cost=0), "patent")
        self.assertNotIn("total", unknown)
        self.assertIn("patent_potential_income", unknown["missing"])
        self.assertGreater(Decimal(unknown["range"]["min"]), 0)

    def test_ausn_has_no_fixed_contributions_but_has_accident_insurance_with_staff(self):
        no_staff = row(comparison(), "ausn_income")
        self.assertEqual(no_staff["total"], "96000.00")
        with_staff = row(comparison(employees=2), "ausn_income")
        self.assertEqual(with_staff["components"]["own_contributions"], "0.00")
        self.assertEqual(with_staff["components"]["employer_contributions"], "2959.00")
        self.assertEqual(with_staff["total"], "98959.00")
        self.assertEqual(with_staff["status"], "conditional")

    def test_ausn_excludes_recognised_nonparticipating_region_only(self):
        baseline = comparison()
        result = comparison(region='Нижегородская область')
        for name in ('ausn_income', 'ausn_profit'):
            item = row(result, name)
            self.assertEqual(item['status'], 'excluded')
            self.assertFalse(item['comparable'])
            self.assertNotIn('total', item)
            self.assertNotIn('range', item)
            self.assertEqual(item['region_check']['code'], '52')
            self.assertIn('30.09.2026', item['reason'])
        for name in ('npd', 'usn_income', 'usn_profit', 'patent', 'osno'):
            self.assertEqual(row(result, name), row(baseline, name))

    def test_ausn_unknown_region_has_missing_input_and_never_ranks(self):
        result = comparison(region='неизвестная область', npd_deduction_remaining=0,
                            npd_activity_eligible=True, new_ip=True, usn_income_rate=6)
        for name in ('ausn_income', 'ausn_profit'):
            item = row(result, name)
            self.assertEqual(item['status'], 'needs_input')
            self.assertIn('region', item['missing'])
            self.assertFalse(item['comparable'])
            self.assertIsNone(item['region_check']['available'])
            self.assertNotIn(name, result['recommendation']['compared_ids'])
        self.assertEqual(row(result, 'ausn_income')['total'], '96000.00')

    def test_ausn_region_codes_aliases_and_effective_dates(self):
        for value in ('63', 63, 'Самара', 'Самарская обл.'):
            item = row(comparison(region=value), 'ausn_income')
            self.assertTrue(item['comparable'])
            self.assertEqual(item['region_check']['code'], '63')
            self.assertEqual(item['total'], '96000.00')
        before = compare_taxes({**BASE, 'region': 'ЯНАО'}, today=date(2026, 3, 31))
        after = compare_taxes({**BASE, 'region': 'ЯНАО'}, today=date(2026, 4, 1))
        self.assertEqual(row(before, 'ausn_income')['status'], 'excluded')
        self.assertNotEqual(row(after, 'ausn_income')['status'], 'excluded')
        self.assertIn('2026-04-01', row(after, 'ausn_income')['region_check']['starts_on'])

    def test_ausn_unverified_future_region_snapshot_cannot_rank(self):
        result = compare_taxes({**BASE, 'year': 2026}, today=date(2026, 10, 1))
        item = row(result, 'ausn_income')
        self.assertEqual(item['status'], 'needs_input')
        self.assertFalse(item['comparable'])
        self.assertIn('region', item['missing'])
        self.assertEqual(item['region_check']['verified_on'], '2026-09-30')

    def test_ausn_profit_annual_minimum_is_only_lower_bound(self):
        item = row(comparison(expenses="1100000", expenses_confirmed=True), "ausn_profit")
        self.assertNotIn("total", item)
        self.assertEqual(item["range"]["min"], "36000.00")
        self.assertEqual(item["range"]["max"], "240000.00")
        self.assertIn("monthly_income_expenses", item["missing"])

    def test_osno_is_progressive_and_requires_vat_residency_other_income(self):
        incomplete = row(comparison(), "osno")
        self.assertNotIn("total", incomplete)
        self.assertIn("tax_resident", incomplete["missing"])
        result = comparison(revenue="3000000", expenses="0", expenses_confirmed=True,
                            expenses_include_contributions=True, tax_resident=True,
                            other_income="0", vat_amount="200000")
        item = row(result, "osno")
        self.assertEqual(item["components"]["tax"], "402000.00")
        self.assertEqual(item["components"]["own_contributions"], "84390.00")
        self.assertEqual(item["total"], "686390.00")
        higher = row(comparison(revenue="1200000", expenses=0, expenses_confirmed=True,
                                expenses_include_contributions=True, tax_resident=True,
                                other_income="5000000", vat_amount=0), "osno")
        self.assertEqual(higher["components"]["tax"], "216000.00")

    def test_recommendation_compares_only_complete_scenarios_and_is_conditional(self):
        self.assertIsNone(comparison()["recommendation"])
        result = comparison(npd_deduction_remaining=0, npd_activity_eligible=True, new_ip=True, usn_income_rate=6)
        recommendation = result["recommendation"]
        self.assertEqual(recommendation["ids"], ["npd"])
        self.assertEqual(set(recommendation["compared_ids"]), {"npd", "usn_income", "ausn_income"})
        self.assertIn("Право на режим надо подтвердить", recommendation["text"])
        self.assertNotIn("самый выгодный", recommendation["text"])

    def test_npd_recommendation_requires_explicit_activity_confirmation(self):
        result = comparison(activity='Перепродаю покупные торты через интернет',
                            npd_deduction_remaining=0, new_ip=True, usn_income_rate=6)
        item = row(result, 'npd')
        self.assertEqual(item['status'], 'conditional')
        self.assertEqual(item['total'], '48000.00')
        self.assertFalse(item['comparable'])
        self.assertIn('npd_activity_eligible', item['missing'])
        self.assertNotIn('npd', result['recommendation']['compared_ids'])
        rejected = row(comparison(npd_activity_eligible=False), 'npd')
        self.assertEqual(rejected['status'], 'excluded')
        self.assertNotIn('total', rejected)
        confirmed = row(comparison(npd_activity_eligible=True, npd_deduction_remaining=0), 'npd')
        self.assertTrue(confirmed['comparable'])
        # Missing/false activity evidence does not get inferred from a low tax.
        for flag in (None, False):
            result = comparison(npd_activity_eligible=flag, npd_deduction_remaining=0,
                                new_ip=True, usn_income_rate=6)
            self.assertNotIn('npd', result['recommendation']['ids'])

    def test_null_activity_and_region_do_not_become_literal_none_strings(self):
        result = comparison(activity=None, region=None, npd_deduction_remaining=0,
                            new_ip=True, usn_income_rate=6)
        self.assertIsNone(result["recommendation"])
        for item in result["rows"]:
            self.assertFalse(item["comparable"])
            self.assertIn("activity", item["missing"])
            self.assertIn("region", item["missing"])

    def test_render_includes_all_regimes_uncertainty_sources_and_fits_max(self):
        for result in (comparison(), comparison(employees=2), comparison(npd_deduction_remaining=0, new_ip=True, usn_income_rate=6)):
            text = render_comparison(result)
            self.assertLessEqual(len(text), 3700)
            for title in ("НПД", "УСН «Доходы»", "УСН «Доходы − расходы»", "ПСН", "АУСН «Доходы»", "АУСН «Доходы − расходы»", "ОСНО"):
                self.assertIn(title, text)
            self.assertIn("https://www.nalog.gov.ru/new2026/", text)
            self.assertIn("не подтверждено", text)

    def test_tax_details_complete_scenario_has_labeled_components_and_version(self):
        result = comparison(npd_activity_eligible=True, npd_deduction_remaining=0)
        text = render_tax_details(result, 'npd')
        self.assertIn('Итог указанного сценария: 48 000.00 ₽', text)
        self.assertIn('Налог режима: 48 000.00 ₽', text)
        self.assertIn('Взносы ИП за себя: 0.00 ₽', text)
        self.assertIn('НДС: 0.00 ₽', text)
        self.assertIn('перепродажа', text)
        self.assertIn(result['rules_version'], text)
        self.assertIn(result['verified_on'], text)
        self.assertIn('https://npd.nalog.ru/', text)
        self.assertNotIn('https://ausn.nalog.gov.ru/', text)
        self.assertLessEqual(len(text), 3900)

    def test_tax_details_preserve_partial_amount_ranges_and_human_missing_labels(self):
        result = comparison()
        income = render_tax_details(result, 'usn_income')
        self.assertIn('Частичная сумма: 72 000.00 ₽', income)
        self.assertIn('общего итога нет', income)
        self.assertIn('региональная ставка УСН доходы', income)
        self.assertIn('годовой НДС', income)
        self.assertNotIn('vat_amount', income)
        self.assertNotIn('Итог указанного сценария:', income)
        profit = render_tax_details(result, 'usn_profit')
        self.assertIn('Диапазон: 183 781.50 ₽ – 198 390.00 ₽', profit)
        self.assertIn('неизвестные НДС/взносы работников не включены', profit)
        self.assertIn('включены ли признанные взносы', profit)
        self.assertIn('https://www.nalog.gov.ru/new2026/', profit)
        self.assertLessEqual(len(income), 3900)
        self.assertLessEqual(len(profit), 3900)
        long_url = 'https://www.nalog.gov.ru/' + 'a' * 2000
        row(result, 'usn_profit')['sources'] = [{'title': 'ФНС России', 'url': long_url}]
        long_source = render_tax_details(result, 'usn_profit')
        self.assertIn(long_url, long_source)
        self.assertIn('неизвестные НДС/взносы работников не включены', long_source)
        self.assertIn('Минимальный налог 1%', long_source)
        self.assertLessEqual(len(long_source), 3900)

    def test_tax_details_exclusion_and_patent_alias_do_not_show_fictitious_totals(self):
        result = comparison(previous_revenue='21000000')
        text = render_tax_details(result, 'psn')
        self.assertIn('Режим исключён', text)
        self.assertIn('Доход за 2025 год выше', text)
        self.assertIn('ПСН в 2026 году недоступна', text)
        self.assertNotIn('Итог указанного сценария:', text)
        self.assertIn('https://www.nalog.gov.ru/rn77/taxation/taxes/patent/', text)
        self.assertLessEqual(len(text), 3900)


if __name__ == "__main__":
    unittest.main()
