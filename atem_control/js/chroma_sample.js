/* chroma_sample.js — the USK chroma sample-box Alpine component.
 *
 * x-data="chromaSampleBox(k)" on the chroma panel's sample preview box:
 * draggable square cursor + size slider over the video frame, pushing
 * sample position/size through the store's setUSKChromaSample* methods
 * (read via Alpine.store('atem') at event time). Exposed on window for
 * the template.
 */

// Chroma Sample Box Alpine Component - DEFINED OUTSIDE ATEMControl
function chromaSampleBox(uskIndex) {
    return {
        isDragging: false,
        startX: 0,
        startY: 0,
        startCursorX: 0,
        startCursorY: 0,
        
        get cursorStyle() {
            const store = Alpine.store('atem');
            const x = (store.state.mes[store.activeMe].usk?.data?.[uskIndex]?.chroma_sample_cursor_x || 0.5) * 100;
            const y = (store.state.mes[store.activeMe].usk?.data?.[uskIndex]?.chroma_sample_cursor_y || 0.5) * 100;
            const size = (store.state.mes[store.activeMe].usk?.data?.[uskIndex]?.chroma_sample_size || 0.1) * 100;
            
            return `
                left: ${x}%;
                top: ${y}%;
                width: ${size}%;
                height: ${size}%;
                transform: translate(-50%, -50%);
            `;
        },
        
        startDrag(e) {
            e.preventDefault();
            this.isDragging = true;
            
            const store = Alpine.store('atem');
            const rect = e.currentTarget.parentElement.getBoundingClientRect();
            
            // Get starting positions
            this.startX = e.type.includes('touch') ? e.touches[0].clientX : e.clientX;
            this.startY = e.type.includes('touch') ? e.touches[0].clientY : e.clientY;
            this.startCursorX = store.state.mes[store.activeMe].usk?.data?.[uskIndex]?.chroma_sample_cursor_x || 0.5;
            this.startCursorY = store.state.mes[store.activeMe].usk?.data?.[uskIndex]?.chroma_sample_cursor_y || 0.5;
            
            // Add event listeners
            const moveHandler = (e) => this.handleDrag(e, rect);
            const endHandler = () => this.endDrag(moveHandler, endHandler);
            
            document.addEventListener('mousemove', moveHandler);
            document.addEventListener('mouseup', endHandler);
            document.addEventListener('touchmove', moveHandler, { passive: false });
            document.addEventListener('touchend', endHandler);
        },
        
        handleDrag(e, rect) {
            if (!this.isDragging) return;
            e.preventDefault();
            
            const store = Alpine.store('atem');
            const clientX = e.type.includes('touch') ? e.touches[0].clientX : e.clientX;
            const clientY = e.type.includes('touch') ? e.touches[0].clientY : e.clientY;
            
            // Calculate delta
            const deltaX = (clientX - this.startX) / rect.width;
            const deltaY = (clientY - this.startY) / rect.height;
            
            // Update position
            const newX = Math.max(0, Math.min(1, this.startCursorX + deltaX));
            const newY = Math.max(0, Math.min(1, this.startCursorY + deltaY));
            
            store.setUSKChromaSampleCursorX(uskIndex, newX);
            store.setUSKChromaSampleCursorY(uskIndex, newY);
        },
        
        endDrag(moveHandler, endHandler) {
            this.isDragging = false;
            document.removeEventListener('mousemove', moveHandler);
            document.removeEventListener('mouseup', endHandler);
            document.removeEventListener('touchmove', moveHandler);
            document.removeEventListener('touchend', endHandler);
        }
    };
}

// Make chromaSampleBox globally available for Alpine
window.chromaSampleBox = chromaSampleBox;
