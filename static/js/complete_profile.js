(function () {
    const panels = {
        uok_student:      document.getElementById('panel_uok'),
        other_university: document.getElementById('panel_other_uni'),
        industry:         document.getElementById('panel_industry'),
    };
    const placeholder = document.getElementById('panel_placeholder');
    const submitBtn   = document.getElementById('submitBtn');
    const radios      = document.querySelectorAll('input[name="participant_type"]');

    function showPanel(type) {
        placeholder.classList.remove('active');
        Object.values(panels).forEach(p => p.classList.remove('active'));
        if (panels[type]) panels[type].classList.add('active');
        submitBtn.disabled = false;
    }

    radios.forEach(function (radio) {
        radio.addEventListener('change', function () {
            if (this.checked) showPanel(this.value);
        });
        // On page reload with validation error, re-activate the right panel
        if (radio.checked) showPanel(radio.value);
    });

    // Prevent double-submit
    document.getElementById('profileForm').addEventListener('submit', function () {
        submitBtn.disabled = true;
        submitBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status"></span>Saving\u2026';
    });
})();
