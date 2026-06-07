document.addEventListener('DOMContentLoaded', () => {
    const cards = document.querySelectorAll('.card');
    cards.forEach(card => {
        card.addEventListener('mouseenter', () => {
            card.style.transform = 'translateY(-5px)';
        });
        card.addEventListener('mouseleave', () => {
            card.style.transform = 'translateY(0)';
        });
    });
});

document.addEventListener('DOMContentLoaded', () => {
    const burger = document.getElementById('burger');
    const mobileMenu = document.getElementById('mobile-menu');

    if (burger && mobileMenu) {
        burger.addEventListener('click', (e) => {
            e.stopPropagation();
            burger.classList.toggle('active');
            mobileMenu.classList.toggle('active');
            document.body.style.overflow = mobileMenu.classList.contains('active') ? 'hidden' : 'auto';
        });

        document.addEventListener('click', (e) => {
            if (!e.target.closest('.mobile-menu') && !e.target.closest('.burger')) {
                mobileMenu.classList.remove('active');
                burger.classList.remove('active');
                document.body.style.overflow = 'auto';
            }
        });

        window.addEventListener('resize', () => {
            if (window.innerWidth > 768) {
                mobileMenu.classList.remove('active');
                burger.classList.remove('active');
                document.body.style.overflow = 'auto';
            }
        });
    }

    const cards = document.querySelectorAll('.card');
    cards.forEach(card => {
        card.addEventListener('mouseenter', () => {
            card.style.transform = 'translateY(-8px)';
        });
        card.addEventListener('mouseleave', () => {
            card.style.transform = 'translateY(0)';
        });
    });
});