// ===========================================================================
// Realtime-slider drag-handler factory
// ===========================================================================
// Every continuous slider on the control surface drives the ATEM through
// the same method quadruplet, bound from the templates by name:
//
//   set<Name>(..., value)      fire-and-forget single send — number inputs
//                              and +/- spinner buttons
//   start<Name>Drag(...)       arms the dragRegistry guard for the path
//   drag<Name>(..., value)     optimistic state write + throttled send
//   release<Name>(..., value)  final state write + flush, with snap-to-step
//                              and the post-release wait-for-echo mute
//
// These were 31 hand-copied quadruplets (~1,000 lines); they are now
// generated from the descriptor tables below. Each row reproduces its
// original handler's exact parameters: dragRegistry path, command verb,
// payload key, and release step.
//
// SNAP-BACK WARNING: the dragRegistry path built here must stay in sync
// with the per-field guard lists in updateState() (the localUskLuma /
// localUskPattern / localUskDVE / localUskChroma / localDSK / localStinger
// / localDVETx captures). updateState restores the operator's local value
// while dragRegistry.shouldSkip() says the echo hasn't landed; a path the
// guard lists don't cover reintroduces the snap-back-on-release failure
// mode. When adding a row here, add the field to the matching guard list.

// USK-indexed sliders. dragRegistry path:
// `atem:me.${activeMe}.usk.${uskIndex}.<field>` (M/E-indexed since Stage
// 4A so an M/E switch can't cross-wire echo guards); state target:
// state.mes[activeMe].usk.data[uskIndex].<field>; wire payload:
// { key_index: uskIndex, <payloadKey>: value } — cmd() stamps the active
// M/E index onto every payload. Wire-scale notes carried
// over from the hand-written handlers: luma + pattern clip/gain/size are
// u16 x10..x100 of percent with a wire grid finer than the UI step, so
// the release tolerance defaults to the step; advanced-chroma params use
// step 0.001 to match the sliders' HTML step attribute.
const USK_SLIDER_DESCRIPTORS = [
    { name: 'USKLumaClip',                field: 'luma_clip',                 verb: 'set_usk_luma_clip',                 payloadKey: 'clip',             step: 0.1 },
    { name: 'USKLumaGain',                field: 'luma_gain',                 verb: 'set_usk_luma_gain',                 payloadKey: 'gain',             step: 0.1 },
    { name: 'USKPatternSize',             field: 'pattern_size',              verb: 'set_usk_pattern_size',              payloadKey: 'size',             step: 0.1 },
    { name: 'USKPatternSymmetry',         field: 'pattern_symmetry',          verb: 'set_usk_pattern_symmetry',          payloadKey: 'symmetry',         step: 0.1 },
    { name: 'USKPatternSoftness',         field: 'pattern_softness',          verb: 'set_usk_pattern_softness',          payloadKey: 'softness',         step: 0.1 },
    { name: 'USKDVERotation',             field: 'dve_rotation',              verb: 'set_usk_dve_rotation',              payloadKey: 'rotation',         step: 1 },
    { name: 'USKDVEBorderHue',            field: 'dve_border_hue',            verb: 'set_usk_dve_border_hue',            payloadKey: 'hue',              step: 0.1 },
    { name: 'USKDVEBorderSaturation',     field: 'dve_border_saturation',     verb: 'set_usk_dve_border_saturation',     payloadKey: 'saturation',       step: 0.1 },
    { name: 'USKDVEBorderLuma',           field: 'dve_border_luma',           verb: 'set_usk_dve_border_luma',           payloadKey: 'luma',             step: 0.1 },
    { name: 'USKDVEBorderOuterWidth',     field: 'dve_border_outer_width',    verb: 'set_usk_dve_border_outer_width',    payloadKey: 'outerWidth',       step: 0.01 },
    { name: 'USKDVEBorderInnerWidth',     field: 'dve_border_inner_width',    verb: 'set_usk_dve_border_inner_width',    payloadKey: 'innerWidth',       step: 0.01 },
    { name: 'USKDVEBorderOuterSoftness',  field: 'dve_border_outer_softness', verb: 'set_usk_dve_border_outer_softness', payloadKey: 'outerSoftness',    step: 1 },
    { name: 'USKDVEBorderInnerSoftness',  field: 'dve_border_inner_softness', verb: 'set_usk_dve_border_inner_softness', payloadKey: 'innerSoftness',    step: 1 },
    { name: 'USKDVEBorderOpacity',        field: 'dve_border_opacity',        verb: 'set_usk_dve_border_opacity',        payloadKey: 'opacity',          step: 1 },
    { name: 'USKDVEBorderBevelPosition',  field: 'dve_border_bevel_position', verb: 'set_usk_dve_border_bevel_position', payloadKey: 'bevelPosition',    step: 1 },
    { name: 'USKDVEBorderBevelSoftness',  field: 'dve_border_bevel_softness', verb: 'set_usk_dve_border_bevel_softness', payloadKey: 'bevelSoftness',    step: 1 },
    { name: 'USKChromaForeground',        field: 'chroma_foreground',         verb: 'set_usk_chroma_foreground',         payloadKey: 'foreground',       step: 0.001 },
    { name: 'USKChromaBackground',        field: 'chroma_background',         verb: 'set_usk_chroma_background',         payloadKey: 'background',       step: 0.001 },
    { name: 'USKChromaKeyEdge',           field: 'chroma_key_edge',           verb: 'set_usk_chroma_key_edge',           payloadKey: 'keyEdge',          step: 0.001 },
    { name: 'USKChromaSpill',             field: 'chroma_spill',              verb: 'set_usk_chroma_spill',              payloadKey: 'spill',            step: 0.001 },
    { name: 'USKChromaFlareSuppression',  field: 'chroma_flare_suppression',  verb: 'set_usk_chroma_flare_suppression',  payloadKey: 'flareSuppression', step: 0.001 },
    { name: 'USKChromaBrightness',        field: 'chroma_brightness',         verb: 'set_usk_chroma_brightness',         payloadKey: 'brightness',       step: 0.001 },
    { name: 'USKChromaContrast',          field: 'chroma_contrast',           verb: 'set_usk_chroma_contrast',           payloadKey: 'contrast',         step: 0.001 },
    { name: 'USKChromaSaturation',        field: 'chroma_saturation',         verb: 'set_usk_chroma_saturation',         payloadKey: 'saturation',       step: 0.001 },
    { name: 'USKChromaRed',               field: 'chroma_red',                verb: 'set_usk_chroma_red',                payloadKey: 'red',              step: 0.001 },
    { name: 'USKChromaGreen',             field: 'chroma_green',              verb: 'set_usk_chroma_green',              payloadKey: 'green',            step: 0.001 },
    { name: 'USKChromaBlue',              field: 'chroma_blue',               verb: 'set_usk_chroma_blue',               payloadKey: 'blue',             step: 0.001 },
];

// DSK-indexed sliders (Stage 3B — the settings DSK sections render via
// x-for off topology.dsks). dragRegistry path: `atem:dsk.${dskIndex}.<field>`;
// state target: state.dsks[dskIndex].<field>; wire payload:
// { dsk: dskIndex, <payloadKey>: value }. Same DSK clip/gain wire scale
// as before the restructure (u16 x10 of percent).
const DSK_SLIDER_DESCRIPTORS = [
    { name: 'DSKClip', field: 'clip', verb: 'set_dsk_clip', payloadKey: 'clip', step: 0.1 },
    { name: 'DSKGain', field: 'gain', verb: 'set_dsk_gain', payloadKey: 'gain', step: 0.1 },
];

// Non-indexed sliders (stinger, DVE-transition). `container` resolves
// the state object the field lives on — it mirrors the original handlers'
// optional-chained existence check, so a missing sub-object means "skip the
// optimistic write, still send". All four generate a set<Name>() since the
// Phase-3 slice-4 dedup: the settings sliders' numeric entries and caret
// nudges call it (they used to reach for $store.atem.send() directly).
// Stinger and DVE-tx share the USK-luma wire scale (x10 of
// percent); DVE-tx was fixed in Bug D commit 1 (7da176c).
// ``path`` is a function of the store since Stage 4A — these transition
// params are per-M/E, so the registry key embeds the active M/E index.
const GLOBAL_SLIDER_DESCRIPTORS = [
    { name: 'StingerClip', field: 'clip', verb: 'set_stinger_clip', payloadKey: 'clip', step: 0.1, setter: true,
      path: (store) => `atem:me.${store.activeMe}.transition.stinger.clip`, container: (store) => store.state.mes[store.activeMe].transition?.stinger },
    { name: 'StingerGain', field: 'gain', verb: 'set_stinger_gain', payloadKey: 'gain', step: 0.1, setter: true,
      path: (store) => `atem:me.${store.activeMe}.transition.stinger.gain`, container: (store) => store.state.mes[store.activeMe].transition?.stinger },
    { name: 'DVEClip',     field: 'clip', verb: 'set_dve_clip',     payloadKey: 'clip', step: 0.1, setter: true,
      path: (store) => `atem:me.${store.activeMe}.transition.dve.clip`,     container: (store) => store.state.mes[store.activeMe].transition?.dve },
    { name: 'DVEGain',     field: 'gain', verb: 'set_dve_gain',     payloadKey: 'gain', step: 0.1, setter: true,
      path: (store) => `atem:me.${store.activeMe}.transition.dve.gain`,     container: (store) => store.state.mes[store.activeMe].transition?.dve },
];

// Builds the handler methods spread into the Alpine store literal below.
// All generated functions are regular (non-arrow) so `this` binds to the
// store when Alpine invokes them as methods.
export function buildSliderHandlers() {
    const handlers = {};

    for (const d of USK_SLIDER_DESCRIPTORS) {
        // M/E-indexed registry path (Stage 4A) so switching M/Es can't
        // collide echo guards across M/Es. Must stay in sync with the
        // guard-side strings in updateState.
        const path = (store, i) => `atem:me.${store.activeMe}.usk.${i}.${d.field}`;
        const writeState = function (uskIndex, parsed) {
            if (this.state.mes[this.activeMe].usk?.data?.[uskIndex]) {
                this.state.mes[this.activeMe].usk.data[uskIndex][d.field] = parsed;
            }
        };
        const sendValue = function (uskIndex, v) {
            this.send(d.verb, { key_index: uskIndex, [d.payloadKey]: v });
        };
        handlers[`set${d.name}`] = function (uskIndex, value) {
            const parsed = parseFloat(value);
            writeState.call(this, uskIndex, parsed);
            sendValue.call(this, uskIndex, parsed);
        };
        handlers[`start${d.name}Drag`] = function (uskIndex) {
            window.ATEMControl.dragRegistry.startDrag(path(this, uskIndex));
        };
        handlers[`drag${d.name}`] = function (uskIndex, value) {
            const parsed = parseFloat(value);
            writeState.call(this, uskIndex, parsed);
            window.ATEMControl.RealtimeSlider.drag(path(this, uskIndex), parsed, (v) => {
                sendValue.call(this, uskIndex, v);
            });
        };
        handlers[`release${d.name}`] = function (uskIndex, value) {
            const parsed = parseFloat(value);
            writeState.call(this, uskIndex, parsed);
            window.ATEMControl.RealtimeSlider.release(path(this, uskIndex), parsed, (v) => {
                sendValue.call(this, uskIndex, v);
            }, { step: d.step });
        };
    }

    for (const d of DSK_SLIDER_DESCRIPTORS) {
        const path = (i) => `atem:dsk.${i}.${d.field}`;
        const writeState = function (dskIndex, parsed) {
            if (this.state.dsks?.[dskIndex]) {
                this.state.dsks[dskIndex][d.field] = parsed;
            }
        };
        const sendValue = function (dskIndex, v) {
            this.send(d.verb, { dsk: dskIndex, [d.payloadKey]: v });
        };
        handlers[`set${d.name}`] = function (dskIndex, value) {
            const parsed = parseFloat(value);
            writeState.call(this, dskIndex, parsed);
            sendValue.call(this, dskIndex, parsed);
        };
        handlers[`start${d.name}Drag`] = function (dskIndex) {
            window.ATEMControl.dragRegistry.startDrag(path(dskIndex));
        };
        handlers[`drag${d.name}`] = function (dskIndex, value) {
            const parsed = parseFloat(value);
            writeState.call(this, dskIndex, parsed);
            window.ATEMControl.RealtimeSlider.drag(path(dskIndex), parsed, (v) => {
                sendValue.call(this, dskIndex, v);
            });
        };
        handlers[`release${d.name}`] = function (dskIndex, value) {
            const parsed = parseFloat(value);
            writeState.call(this, dskIndex, parsed);
            window.ATEMControl.RealtimeSlider.release(path(dskIndex), parsed, (v) => {
                sendValue.call(this, dskIndex, v);
            }, { step: d.step });
        };
    }

    for (const d of GLOBAL_SLIDER_DESCRIPTORS) {
        const writeState = function (parsed) {
            const target = d.container(this);
            if (target) {
                target[d.field] = parsed;
            }
        };
        const sendValue = function (v) {
            this.send(d.verb, { [d.payloadKey]: v });
        };
        if (d.setter) {
            handlers[`set${d.name}`] = function (value) {
                const parsed = parseFloat(value);
                writeState.call(this, parsed);
                sendValue.call(this, parsed);
            };
        }
        handlers[`start${d.name}Drag`] = function () {
            window.ATEMControl.dragRegistry.startDrag(d.path(this));
        };
        handlers[`drag${d.name}`] = function (value) {
            const parsed = parseFloat(value);
            writeState.call(this, parsed);
            window.ATEMControl.RealtimeSlider.drag(d.path(this), parsed, (v) => {
                sendValue.call(this, v);
            });
        };
        handlers[`release${d.name}`] = function (value) {
            const parsed = parseFloat(value);
            writeState.call(this, parsed);
            window.ATEMControl.RealtimeSlider.release(d.path(this), parsed, (v) => {
                sendValue.call(this, v);
            }, { step: d.step });
        };
    }

    return handlers;
}
