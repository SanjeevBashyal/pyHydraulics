import gspread
from google.oauth2.service_account import Credentials

class GoogleSheetsClient:
    def __init__(self, credentials_file="master_credentials.json"):
        self.scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        self.creds = Credentials.from_service_account_file(credentials_file, scopes=self.scopes)
        self.client = gspread.authorize(self.creds)

    def get_workbook(self, sheet_id):
        return self.client.open_by_key(sheet_id)

    def get_worksheet(self, sheet_id, worksheet_name):
        workbook = self.get_workbook(sheet_id)
        return workbook.worksheet(worksheet_name)

    def get_all_values(self, sheet_id, worksheet_name):
        worksheet = self.get_worksheet(sheet_id, worksheet_name)
        return worksheet.get_all_values()

    def update_range(self, sheet_id, worksheet_name, cell_range, values, value_input_option="USER_ENTERED"):
        worksheet = self.get_worksheet(sheet_id, worksheet_name)
        worksheet.update(values=values, range_name=cell_range, value_input_option=value_input_option)

    def format_range(self, sheet_id, worksheet_name, cell_range, formats):
        worksheet = self.get_worksheet(sheet_id, worksheet_name)
        worksheet.format(cell_range, formats)
        
    def batch_update(self, sheet_id, requests):
        workbook = self.get_workbook(sheet_id)
        workbook.batch_update({"requests": requests})