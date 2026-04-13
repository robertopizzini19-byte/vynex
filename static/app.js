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
});
