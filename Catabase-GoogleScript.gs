//Downloads the images at a discord media link to our google drive.
function downloadImageDiscords() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName("TCBPicsInput");
  var folder = DriveApp.getFolderById("1zLMeRLStBuIxdV7mmT_SHWR1o_-CE7T9");
  if (!sheet) return;

  var lastRow = sheet.getLastRow();
  var links = sheet.getRange(2, 1, lastRow - 1).getValues();   // column A
  var done   = sheet.getRange(2, 4, lastRow - 1).getValues();   // column D
  var counterCell = sheet.getRange("G2");
  var counter = Number(counterCell.getValue() || 0);

  for (var i = 0; i < links.length; i++) {
    var discordUrl = links[i][0];
    var existingUrl = done[i][0];  // already pulled into memory

    if (existingUrl) continue;                                // skip completed
    if (!discordUrl || !discordUrl.includes("discordapp.com")) continue;

    var rowIndex = i + 2;
    try {
      var response = UrlFetchApp.fetch(discordUrl, { muteHttpExceptions: true });
      if (response.getResponseCode() !== 200) continue;
      var fileBlob = response.getBlob();

      var serialNumber = String(counter + 1).padStart(4, '0');
      var newFileName = "sn" + serialNumber + ".jpg";
      Logger.log("Processing row " + rowIndex + ": " + newFileName);

      var compressedBlob = optimizeImage(fileBlob);
      var newFile = folder.createFile(compressedBlob).setName(newFileName);
      var newFileUrl = newFile.getUrl();

      // write results back
      sheet.getRange(rowIndex, 4).setValue(newFileUrl); // D
      sheet.getRange(rowIndex, 6).setValue(serialNumber); // F
      counter++;
      counterCell.setValue(counter);

    } catch (e) {
      Logger.log("Error on row " + rowIndex + ": " + e.message);
    }
  }
  Logger.log("Done. Total processed: " + counter);
}

//Compresses images people send to discord. Depending if they are a discord Nitro user or not, the images can be up 10mb. this keeps em
function optimizeImage(imageBlob) {
  // skip small files
  var size = imageBlob.getBytes().length;
  if (size <= 750 * 1024) {
    return imageBlob;
  }

  // your four Tinify API keys
  var apiKeys = [
    "wZfgtRrSRBHLmH9gpR1SLt3gd7cPZsG5",
    "dVpBXYqHgMPxdcvzHFDp5nTZKTSPKKCK",
    "1V7gPHtlG8mkXJBFtrv6qWSrKYhQ71pk",
    "1KmYZq2xLMs1bJdfPYqdjJ3Qlfth7cNG"
  ];
  var keys = apiKeys.slice();  // work on a copy
  var lastError;

  // keep trying random keys until one works or none left
  while (keys.length) {
    var idx    = Math.floor(Math.random() * keys.length);
    var apiKey = keys[idx];
    try {
      var resp = UrlFetchApp.fetch("https://api.tinify.com/shrink", {
        method: "post",
        contentType: "application/octet-stream",
        payload: imageBlob.getBytes(),
        headers: {
          "Authorization": "Basic " + Utilities.base64Encode(apiKey + ":")
        },
        muteHttpExceptions: true
      });

      if (resp.getResponseCode() === 201) {
        // compression accepted
        var resultUrl = resp.getHeaders()["Location"];
        var compressed = UrlFetchApp.fetch(resultUrl, {muteHttpExceptions: true});
        if (compressed.getResponseCode() === 200) {
          return compressed.getBlob();
        } else {
          throw new Error("Fetching compressed image failed: " 
                          + compressed.getContentText());
        }
      }

      // handle Tinify error (e.g. over limit)
      var errTxt = resp.getContentText();
      if (errTxt.indexOf("Your monthly limit has been exceeded") !== -1) {
        lastError = new Error("Key exceeded monthly limit: " + apiKey);
      } else {
        lastError = new Error("Tinify compression failed: " + errTxt);
      }

    } catch (e) {
      lastError = e;
    }

    // drop the bad key and try another
    keys.splice(idx, 1);
  }

  // nothing left that worked
  throw lastError;
}



//very similar to downloadImageDiscords but with vet pics or whatever. 
function downloadVetDiscords() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName("TCBVetBillInput");
  var folder = DriveApp.getFolderById("1uzvruzqAB8pemnR_OW3qM9oHEof5MyY3"); 
  if (!sheet) {
    Logger.log("Error: Sheet 'TCBVetBillInput' not found!");
    return;
  }
  var lastRow = sheet.getLastRow();
  var data = sheet.getRange("A2:A" + lastRow).getValues();
  var counterCell = sheet.getRange("G2");
  var counter = counterCell.getValue();
  if (counter === "") {
    Logger.log("G2 is empty. Initializing counter to 0.");
    counter = 0;
    counterCell.setValue(counter);
  }

  Logger.log("Starting script. Processing " + (lastRow - 1) + " rows.");
  for (var i = 0; i < data.length; i++) {
    var discordUrl = data[i][0];
    var rowIndex = i + 2;
    var existingUrl = sheet.getRange(rowIndex, 4).getValue();
    if (existingUrl) { continue; }
    if (!discordUrl || !discordUrl.includes("discordapp.com")) { continue; }
    
    try {
      var response = UrlFetchApp.fetch(discordUrl, { muteHttpExceptions: true });
      if (response.getResponseCode() !== 200) { continue; }
      var fileBlob = response.getBlob();
      var serialNumber = String(counter + 1).padStart(4, '0');
      var newFileName = "Vsn" + serialNumber + ".jpg";
      Logger.log("Processing row " + rowIndex + ": Renaming to " + newFileName);
      
      var compressedBlob = optimizeImage(fileBlob);
      
      var newFile = folder.createFile(compressedBlob);
      newFile.setName(newFileName);
      var newFileUrl = newFile.getUrl();
      
      sheet.getRange(rowIndex, 4).setValue(newFileUrl);
      sheet.getRange(rowIndex, 6).setValue(serialNumber);
      
      counter++;
      counterCell.setValue(counter);
    } catch (e) {
      Logger.log("Error processing row " + rowIndex + ": " + e.message);
    }
  }
  Logger.log("Script finished. Total processed: " + counter);
}



//The code below is for manually adding sets of images. Keep a column H in the catabase sheet for this to work properly
//manuallyAddImagesFromFolder just takes each image in a given google drive folder and adds the link to column H of 'TCBPicsInput' in catabase
//Be sure to change folderID and startRow variables before starting.
//copyAndRenameImagesFromH takes the pics in this row and applies our normal naming scheme to them, making them 'indistinguishable' from the normal submissions.

function manuallyAddImagesFromFolder() {
  const folderId = '1s2h_4Pxs0ZFPEVGTIX4ywhWzt-7Ygya5'; //SWAP THIS ID WITH THE ONE OF YOUR NEW IMAGES FOLDER!. 
  //This ID will be found in the URL after: drive.google.com/drive/u/0/folders/ . Just copy the string of numbers and letters and paste it above.


  const sheetId  = '15HtHVB4HfOr9e85EgbOGbz9CBwWnXg3pbCytGTA64P4';//Catabase sheet
  const tabName  = 'TCBPicsInput';
  const startRow = 3664;   //VERY IMPORTANT! Change this to the row you want your links pasted.
  const col       = 8; // column H

  const folder = DriveApp.getFolderById(folderId);
  const sheet  = SpreadsheetApp.openById(sheetId).getSheetByName(tabName);

  // 1) Read existing URLs in col H starting at startRow
  const allH = sheet.getRange("H:H").getValues(); 
  let lastFilled = startRow - 1;
  const seen = {};

  // find last non-empty row and collect seen URLs
  for (let i = startRow - 1; i < allH.length; i++) {
    const url = allH[i][0];
    if (url) {
      lastFilled = i + 1;
      seen[url] = true;
    }
  }

  // 2) Loop through folder files, append any new ones
  let row = lastFilled + 1;
  const files = folder.getFiles();
  while (files.hasNext()) {
    const file = files.next();
    const url  = file.getUrl();
    if (!seen[url]) {
      sheet.getRange(row, col).setValue(url);
      seen[url] = true;
      row++;
    }
  }
}

function copyAndRenameImagesFromH() {
  const FOLDER_ID = '1zLMeRLStBuIxdV7mmT_SHWR1o_-CE7T9';
  const sheet     = SpreadsheetApp.getActiveSpreadsheet()
                                  .getSheetByName('TCBPicsInput');
  const folder    = DriveApp.getFolderById(FOLDER_ID);
  const lastRow   = sheet.getLastRow();
  const counterCell = sheet.getRange('G2');  // merged G2:G4, but value lives in G2

  // pull in H (new links) and F (existing serials)
  const urls    = sheet.getRange(2, 8, lastRow - 1).getValues();
  const serials = sheet.getRange(2, 6, lastRow - 1).getValues();

  // figure out highest existing serial
  let maxSerial = serials.reduce((max, [val]) => {
    const n = parseInt(val, 10);
    return (!isNaN(n) && n > max) ? n : max;
  }, 0);

  urls.forEach((row, i) => {
    const url = row[0];
    // skip if no URL or already processed
    if (!url || serials[i][0]) return;

    // extract the Drive file ID
    const m = url.match(/[-\w]{25,}/);
    if (!m) return;
    const fileId   = m[0];
    const original = DriveApp.getFileById(fileId);

    // increment and build new name
    maxSerial++;
    const sn      = String(maxSerial).padStart(4, '0');
    const newName = 'sn' + sn + '.jpg';

    // make the copy and rename
    const copy   = original.makeCopy(newName, folder);
    const copyUrl = copy.getUrl();

    // write back into sheet
    const rowIndex = i + 2;
    sheet.getRange(rowIndex, 4).setValue(copyUrl); // column D
    sheet.getRange(rowIndex, 6).setValue(sn);      // column F
    counterCell.setValue(sn);                      // update G2

    // log each addition
    Logger.log('sn.' + sn + '.jpg added');
  });
}

