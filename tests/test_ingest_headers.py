import unittest

from mvp_task.ingest import read_upload


class TableUploadTest(unittest.TestCase):
    def test_wrong_table_reports_detected_section(self):
        content = b"record_id,date,employee,plan_docs,fact_docs\nD-1,2026-10-05,Test,5,4\n"
        with self.assertRaisesRegex(ValueError, "Похоже, выбран файл таблицы «Документация»"):
            read_upload("docs.csv", content, expected_source="preparation")

    def test_missing_column_is_named(self):
        content = b"record_id,date,batch_id,employee,plan_qty\nP-1,2026-10-05,LOT-1,Test,5\n"
        with self.assertRaisesRegex(ValueError, "fact_qty"):
            read_upload("prep.csv", content, expected_source="preparation")

    def test_valid_csv_is_accepted_for_its_page(self):
        content = b"record_id,date,batch_id,need_qty\nW-1,2026-10-05,LOT-1,5\n"
        source, rows = read_upload("need.csv", content, expected_source="welding_need")
        self.assertEqual(source, "welding_need")
        self.assertEqual(rows[0][1]["need_qty"], "5")


if __name__ == "__main__":
    unittest.main()
