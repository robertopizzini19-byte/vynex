// VYNEX — JS utilities globali

// Auto-hide alerts dopo 5 secondi
document.addEventListener('DOMContentLoaded', () => {
    const alerts = document.querySelectorAll('.alert-success');
    alerts.forEach(alert => {
        setTimeout(() => {
            alert.style.transition = 'opacity .5s';
            alert.style.opacity = '0';
            setTimeout(() => alert.remove(), 500);
        }, 5000);
    });

    // Cookie banner: mostra se non accettato
    const banner = document.getElementById('cookie-banner');
    const acceptBtn = document.getElementById('cookie-accept');
    if (banner && acceptBtn) {
        try {
            if (!localStorage.getItem('vynex_cookie_ack')) {
                banner.hidden = false;
            }
            acceptBtn.addEventListener('click', () => {
                try { localStorage.setItem('vynex_cookie_ack', '1'); } catch(e) {}
                banner.hidden = true;
            });
        } catch (e) {
            // localStorage disabled — mostra comunque
            banner.hidden = false;
            acceptBtn.addEventListener('click', () => { banner.hidden = true; });
        }
    }

    // Password strength meter
    const pwInput = document.getElementById('password');
    const pwWrap = document.getElementById('pw-strength');
    const pwFill = document.getElementById('pw-strength-fill');
    const pwLabel = document.getElementById('pw-strength-label');
    if (pwInput && pwWrap && pwFill && pwLabel) {
        const scorePassword = (pw) => {
            if (!pw) return 0;
            let score = 0;
            if (pw.length >= 8) score++;
            if (pw.length >= 12) score++;
            if (/[a-z]/.test(pw) && /[A-Z]/.test(pw)) score++;
            if (/\d/.test(pw)) score++;
            if (/[^A-Za-z0-9]/.test(pw)) score++;
            return score;
        };
        pwInput.addEventListener('input', () => {
            const pw = pwInput.value;
            if (!pw) {
                pwWrap.classList.remove('active');
                return;
            }
            pwWrap.classList.add('active');
            const score = scorePassword(pw);
            let level = 'weak', text = 'Debole';
            if (score >= 4) { level = 'strong'; text = 'Forte'; }
            else if (score >= 2) { level = 'medium'; text = 'Media'; }
            pwFill.className = 'pw-strength-fill ' + level;
            pwLabel.className = 'pw-strength-label ' + level;
            pwLabel.textContent = text;
        });
    }
});
