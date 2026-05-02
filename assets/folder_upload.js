(function () {
  function initFolderUpload() {
    const host = document.getElementById("folder-uploader-host");
    if (!host || host.dataset.ready === "1") {
      return;
    }
    host.dataset.ready = "1";
    host.innerHTML = `
      <div class="native-folder-upload">
        <label>Upload folder</label>
        <input id="native-folder-input" class="native-folder-input" type="file" multiple webkitdirectory directory accept=".flp" />
        <label for="native-folder-input" class="file-picker">Choose folder</label>
        <span id="native-folder-name" class="file-picker-name">No folder selected</span>
        <button id="native-folder-button" type="button">Upload</button>
        <div id="native-folder-status" class="upload-status">No folder selected.</div>
      </div>
    `;

    const input = host.querySelector("#native-folder-input");
    const button = host.querySelector("#native-folder-button");
    const status = host.querySelector("#native-folder-status");
    const name = host.querySelector("#native-folder-name");

    input.addEventListener("change", () => {
      const files = Array.from(input.files || []).filter((file) => file.name.toLowerCase().endsWith(".flp"));
      name.textContent = files.length ? `${files.length} FLP files` : "No folder selected";
      status.textContent = files.length ? `${files.length} FLP file(s) ready to upload.` : "No FLP files found in that selection.";
    });

    button.addEventListener("click", async () => {
      const files = Array.from(input.files || []).filter((file) => file.name.toLowerCase().endsWith(".flp"));
      if (!files.length) {
        status.textContent = "Choose a folder containing .flp files first.";
        return;
      }

      button.disabled = true;
      status.textContent = `Uploading ${files.length} FLP file(s)...`;
      const form = new FormData();
      files.forEach((file) => {
        form.append("files", file, file.webkitRelativePath || file.name);
      });

      try {
        const response = await fetch("/api/upload-folder", { method: "POST", body: form });
        const payload = await response.json();
        status.textContent = payload.message || (response.ok ? "Upload started." : "Upload failed.");
        const wakeButton = document.getElementById("upload-started-button");
        if (response.ok && wakeButton) {
          wakeButton.click();
        }
      } catch (error) {
        status.textContent = `Upload failed: ${error}`;
      } finally {
        button.disabled = false;
      }
    });
  }

  const observer = new MutationObserver(initFolderUpload);
  observer.observe(document.documentElement, { childList: true, subtree: true });
  document.addEventListener("DOMContentLoaded", initFolderUpload);
  window.addEventListener("load", initFolderUpload);
})();
