/**
 * Zero-cost UGC pipeline controller.
 *
 * Google Sheets is the control center. When Status is changed to READY,
 * this script writes a small JSON job file into Google Drive. A Google Colab
 * worker polls that queue, generates the video locally with Wan2GP, uploads
 * the MP4 to Drive, and writes the result back to this sheet.
 *
 * No n8n. No paid video API. No paid LLM API.
 */

const CFG = {
  CONFIG_SHEET: 'Config',
  VIDEO_SHEET: 'Videos',
  ROOT_FOLDER_NAME: 'UGC_Automation',
  QUEUE_FOLDER_NAME: 'Queue',
  OUTPUT_FOLDER_NAME: 'Output',
  READY_STATUS: 'READY',
  QUEUED_STATUS: 'QUEUED',
  PROCESSING_STATUS: 'PROCESSING',
  FINISHED_STATUS: 'FINISHED',
  ERROR_STATUS: 'ERROR'
};

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('UGC Automation')
    .addItem('1. Setup / Repair', 'setup')
    .addItem('2. Queue selected row', 'queueSelectedRow')
    .addItem('3. Queue first READY row', 'queueFirstReadyRow')
    .addItem('4. Retry selected row', 'retrySelectedRow')
    .addItem('5. Show configuration', 'showConfig')
    .addToUi();
}

function setup() {
  const ss = SpreadsheetApp.getActive();

  const config = getOrCreateSheet_(ss, CFG.CONFIG_SHEET, [
    ['Key', 'Value'],
    ['Spreadsheet ID', ss.getId()],
    ['Videos Sheet', CFG.VIDEO_SHEET]
  ]);

  const videos = getOrCreateSheet_(ss, CFG.VIDEO_SHEET, [[
    'No',
    'Product',
    'Product Photo',
    'ICP',
    'Product Features',
    'Video Setting',
    'Model',
    'Status',
    'Finished Video',
    'Job ID',
    'Error',
    'Updated At'
  ]]);

  const root = getOrCreateFolder_(DriveApp.getRootFolder(), CFG.ROOT_FOLDER_NAME);
  const queue = getOrCreateFolder_(root, CFG.QUEUE_FOLDER_NAME);
  const output = getOrCreateFolder_(root, CFG.OUTPUT_FOLDER_NAME);

  setConfigValue_(config, 'Spreadsheet ID', ss.getId());
  setConfigValue_(config, 'Videos Sheet', videos.getName());
  setConfigValue_(config, 'Queue Folder ID', queue.getId());
  setConfigValue_(config, 'Output Folder ID', output.getId());

  installEditTrigger_();

  SpreadsheetApp.getUi().alert(
    'Configuración lista.\n\n' +
    'Queue: ' + queue.getUrl() + '\n' +
    'Output: ' + output.getUrl() + '\n\n' +
    'Ahora abre el Colab worker y ejecútalo.'
  );
}

function installedOnEdit(e) {
  try {
    if (!e || !e.range) return;

    const sheet = e.range.getSheet();
    if (sheet.getName() !== CFG.VIDEO_SHEET) return;
    if (e.range.getRow() < 2) return;

    const headers = getHeaders_(sheet);
    const statusCol = headers.indexOf('Status') + 1;
    if (!statusCol || e.range.getColumn() !== statusCol) return;

    const status = String(e.value || '').trim().toUpperCase();
    if (status !== CFG.READY_STATUS) return;

    const row = e.range.getRow();
    const headers = getHeaders_(sheet);
    const jobIdCol = headers.indexOf('Job ID') + 1;
    const statusCol = headers.indexOf('Status') + 1;
    const currentJobId = jobIdCol ? String(sheet.getRange(row, jobIdCol).getValue() || '').trim() : '';
    const currentStatus = statusCol ? String(sheet.getRange(row, statusCol).getValue() || '').trim().toUpperCase() : '';

    // READY on a previously failed/finished row starts a fresh job.
    if (currentJobId && currentStatus === CFG.READY_STATUS) {
      sheet.getRange(row, jobIdCol).clearContent();
      const finishedCol = headers.indexOf('Finished Video') + 1;
      if (finishedCol) sheet.getRange(row, finishedCol).clearContent();
    }

    createJobForRow_(sheet, row);
  } catch (err) {
    console.error(err);
  }
}

function queueSelectedRow() {
  const sheet = SpreadsheetApp.getActiveSheet();
  if (sheet.getName() !== CFG.VIDEO_SHEET) {
    SpreadsheetApp.getUi().alert('Selecciona primero una fila en la hoja Videos.');
    return;
  }
  const row = sheet.getActiveRange().getRow();
  if (row < 2) return;
  const headers = getHeaders_(sheet);
  const jobCol = headers.indexOf('Job ID') + 1;
  const finishedCol = headers.indexOf('Finished Video') + 1;
  if (jobCol) sheet.getRange(row, jobCol).clearContent();
  if (finishedCol) sheet.getRange(row, finishedCol).clearContent();
  setCellByHeader_(sheet, row, 'Status', CFG.READY_STATUS);
  createJobForRow_(sheet, row);
}

function queueFirstReadyRow() {
  const sheet = SpreadsheetApp.getActive().getSheetByName(CFG.VIDEO_SHEET);
  if (!sheet) throw new Error('Videos sheet does not exist. Run setup() first.');
  const headers = getHeaders_(sheet);
  const statusCol = headers.indexOf('Status') + 1;
  if (!statusCol) throw new Error('Status header not found.');
  const lastRow = sheet.getLastRow();
  for (let row = 2; row <= lastRow; row++) {
    const status = String(sheet.getRange(row, statusCol).getValue() || '').trim().toUpperCase();
    if (status === CFG.READY_STATUS) {
      const jobCol = headers.indexOf('Job ID') + 1;
      const finishedCol = headers.indexOf('Finished Video') + 1;
      if (jobCol) sheet.getRange(row, jobCol).clearContent();
      if (finishedCol) sheet.getRange(row, finishedCol).clearContent();
      createJobForRow_(sheet, row);
      return;
    }
  }
  throw new Error('No READY row found in Videos.');
}

function retrySelectedRow() {
  const sheet = SpreadsheetApp.getActiveSheet();
  if (sheet.getName() !== CFG.VIDEO_SHEET) return;
  const row = sheet.getActiveRange().getRow();
  if (row < 2) return;

  const headers = getHeaders_(sheet);
  const col = {};
  headers.forEach((h, i) => col[h] = i + 1);

  sheet.getRange(row, col['Status']).setValue(CFG.READY_STATUS);
  sheet.getRange(row, col['Error']).clearContent();
  sheet.getRange(row, col['Job ID']).clearContent();
  sheet.getRange(row, col['Finished Video']).clearContent();
  createJobForRow_(sheet, row);
}

function createJobForRow_(sheet, row) {
  const headers = getHeaders_(sheet);
  const values = sheet.getRange(row, 1, 1, headers.length).getValues()[0];
  const record = {};
  headers.forEach((h, i) => record[h] = values[i]);

  if (!record.Product) throw new Error('Product está vacío.');
  if (!record['Product Photo']) throw new Error('Product Photo está vacío.');

  const jobId = Utilities.getUuid();
  const queueFolder = DriveApp.getFolderById(getConfigValue_('Queue Folder ID'));

  const job = {
    job_id: jobId,
    spreadsheet_id: SpreadsheetApp.getActive().getId(),
    sheet_name: sheet.getName(),
    row_number: row,
    created_at: new Date().toISOString(),
    product: String(record.Product || ''),
    product_photo: String(record['Product Photo'] || ''),
    icp: String(record.ICP || ''),
    product_features: String(record['Product Features'] || ''),
    video_setting: String(record['Video Setting'] || ''),
    model: String(record.Model || 'AUTO'),
    aspect_ratio: '9:16',
    duration_seconds: 4,
    fps: 24
  };

  const existing = sheet.getRange(row, headers.indexOf('Job ID') + 1).getValue();
  if (existing) {
    sheet.getRange(row, headers.indexOf('Status') + 1).setValue(CFG.QUEUED_STATUS);
    return;
  }

  queueFolder.createFile(
    'job_' + jobId + '.json',
    JSON.stringify(job, null, 2),
    MimeType.PLAIN_TEXT
  );

  setCellByHeader_(sheet, row, 'Job ID', jobId);
  setCellByHeader_(sheet, row, 'Status', CFG.QUEUED_STATUS);
  setCellByHeader_(sheet, row, 'Error', '');
  setCellByHeader_(sheet, row, 'Updated At', new Date());
}

function showConfig() {
  const ss = SpreadsheetApp.getActive();
  const sheet = ss.getSheetByName(CFG.CONFIG_SHEET);
  if (!sheet) {
    SpreadsheetApp.getUi().alert('Ejecuta Setup primero.');
    return;
  }
  const values = sheet.getDataRange().getDisplayValues();
  SpreadsheetApp.getUi().alert(values.map(r => r.join(' = ')).join('\n'));
}

function installEditTrigger_() {
  const ss = SpreadsheetApp.getActive();
  ScriptApp.getProjectTriggers()
    .filter(t => t.getHandlerFunction() === 'installedOnEdit')
    .forEach(t => ScriptApp.deleteTrigger(t));

  ScriptApp.newTrigger('installedOnEdit')
    .forSpreadsheet(ss)
    .onEdit()
    .create();
}

function getOrCreateSheet_(ss, name, defaultValues) {
  let sheet = ss.getSheetByName(name);
  if (!sheet) sheet = ss.insertSheet(name);

  if (sheet.getLastRow() === 0) {
    sheet.getRange(1, 1, defaultValues.length, defaultValues[0].length)
      .setValues(defaultValues);
  }

  return sheet;
}

function getOrCreateFolder_(parent, name) {
  const it = parent.getFoldersByName(name);
  return it.hasNext() ? it.next() : parent.createFolder(name);
}

function setConfigValue_(sheet, key, value) {
  const data = sheet.getDataRange().getValues();
  for (let r = 1; r < data.length; r++) {
    if (String(data[r][0]) === key) {
      sheet.getRange(r + 1, 2).setValue(value);
      return;
    }
  }
  sheet.appendRow([key, value]);
}

function getConfigValue_(key) {
  const sheet = SpreadsheetApp.getActive().getSheetByName(CFG.CONFIG_SHEET);
  if (!sheet) throw new Error('Config sheet no existe. Ejecuta setup().');

  const data = sheet.getDataRange().getValues();
  for (let r = 1; r < data.length; r++) {
    if (String(data[r][0]) === key) return String(data[r][1]);
  }
  throw new Error('Config faltante: ' + key);
}

function getHeaders_(sheet) {
  return sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0]
    .map(v => String(v).trim());
}

function setCellByHeader_(sheet, row, header, value) {
  const headers = getHeaders_(sheet);
  const col = headers.indexOf(header);
  if (col === -1) throw new Error('Header no encontrado: ' + header);
  sheet.getRange(row, col + 1).setValue(value);
}
