const fileInput = document.getElementById("logFile");
const analyzeButton = document.getElementById("analyzeButton");
const status = document.getElementById("status");
const selectedFile = document.getElementById("selectedFile");

fileInput.addEventListener("change", () => {

    if (fileInput.files.length > 0) {

        selectedFile.textContent =
            "Selected: " + fileInput.files[0].name;

        selectedFile.className = "selected-file";
    }

});

analyzeButton.addEventListener("click", async () => {

    const file = fileInput.files[0];

    if (!file) {
        status.textContent = "Please select a Jenkins log file.";
        return;
    }

    analyzeButton.disabled = true;
    analyzeButton.textContent = "Analyzing...";
    status.textContent = "AI agents are analyzing the Jenkins log...";

    const formData = new FormData();
    formData.append("file", file);

    try {

        const response = await fetch(
            "http://127.0.0.1:8000/analyze-log",
            {
                method: "POST",
                body: formData
            }
        );

        const data = await response.json();

        if (!response.ok) {
            throw new Error(
                data.detail || "Analysis failed"
            );
        }

        displayResult(data);

        status.textContent =
            "Analysis completed successfully.";

    } catch (error) {

        status.textContent =
            "Error: " + error.message;

    } finally {

        analyzeButton.disabled = false;
        analyzeButton.textContent = "Analyze Build";
    }
});


function displayResult(data) {

    document.getElementById("result")
        .classList.remove("hidden");

    document.getElementById("failureSummary")
        .textContent = data.failure_summary;

    document.getElementById("rootCause")
        .textContent = data.root_cause;

    document.getElementById("affectedComponent")
        .textContent = data.affected_component;

    document.getElementById("recommendedFix")
        .textContent = data.recommended_fix;

    document.getElementById("confidence")
        .textContent = data.confidence;


    const evidenceList =
        document.getElementById("evidence");

    evidenceList.innerHTML = "";

    data.evidence.forEach(item => {

        const li = document.createElement("li");

        li.textContent = item;

        evidenceList.appendChild(li);

    });
}