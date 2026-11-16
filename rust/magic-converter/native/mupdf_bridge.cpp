#include <cmath>
#include <memory>
#include "mupdf/functions.h"

using namespace mupdf;

extern "C" {

__declspec(dllexport) int mc_mupdf_open(const char* path, fz_document** document, int* pages) noexcept {
    try {
        std::unique_ptr<fz_document, decltype(&ll_fz_drop_document)> handle(
            ll_fz_open_document(path), ll_fz_drop_document);
        if (ll_fz_needs_password(handle.get())) return 2;
        *pages = ll_fz_count_pages(handle.get());
        *document = handle.release();
        return 0;
    } catch (...) {
        return 1;
    }
}

__declspec(dllexport) int mc_mupdf_png(
    fz_document* document, int index, int dpi, fz_buffer** result,
    const unsigned char** data, size_t* size, float* width, float* height) noexcept {
    try {
        std::unique_ptr<fz_page, decltype(&ll_fz_drop_page)> page(
            ll_fz_load_page(document, index), ll_fz_drop_page);
        const auto box = ll_fz_bound_page(page.get());
        *width = box.x1 - box.x0;
        *height = box.y1 - box.y0;
        const float scale = dpi / 72.0f;
        const double pixels = std::ceil(*width * scale + 1) * std::ceil(*height * scale + 1);
        if (!std::isfinite(pixels) || *width <= 0 || *height <= 0 || pixels > 64000000) return 3;

        std::unique_ptr<fz_display_list, decltype(&ll_fz_drop_display_list)> display(
            ll_fz_new_display_list_from_page(page.get()), ll_fz_drop_display_list);
        std::unique_ptr<fz_pixmap, decltype(&ll_fz_drop_pixmap)> image(
            ll_fz_new_pixmap_from_display_list(display.get(), ll_fz_scale(scale, scale), ll_fz_device_rgb(), 0),
            ll_fz_drop_pixmap);
        ll_fz_set_pixmap_resolution(image.get(), dpi, dpi);
        std::unique_ptr<fz_buffer, decltype(&ll_fz_drop_buffer)> png(
            ll_fz_new_buffer(4096), ll_fz_drop_buffer);
        {
            std::unique_ptr<fz_output, decltype(&ll_fz_drop_output)> output(
                ll_fz_new_output_with_buffer(png.get()), ll_fz_drop_output);
            ll_fz_write_pixmap_as_png(output.get(), image.get());
            ll_fz_close_output(output.get());
        }
        unsigned char* contents = nullptr;
        *size = ll_fz_buffer_storage(png.get(), &contents);
        *data = contents;
        *result = png.release();
        return 0;
    } catch (...) {
        return 1;
    }
}

__declspec(dllexport) void mc_mupdf_close(fz_document* document) noexcept {
    ll_fz_drop_document(document);
}

__declspec(dllexport) int mc_mupdf_region(
    fz_document* document, int index, const float* requested, int dpi,
    fz_buffer** result, const unsigned char** data, size_t* size, int* origin) noexcept {
    try {
        std::unique_ptr<fz_page, decltype(&ll_fz_drop_page)> page(
            ll_fz_load_page(document, index), ll_fz_drop_page);
        const auto transform = ll_fz_scale(dpi / 72.0f, dpi / 72.0f);
        const auto bounds = ll_fz_intersect_rect(ll_fz_bound_page(page.get()),
            fz_rect{requested[0], requested[1], requested[2], requested[3]});
        const auto area = ll_fz_round_rect(ll_fz_transform_rect(bounds, transform));
        const auto width = static_cast<long long>(area.x1) - area.x0;
        const auto height = static_cast<long long>(area.y1) - area.y0;
        if (width <= 0 || height <= 0 || width > 40000000 / height) return 3;

        std::unique_ptr<fz_pixmap, decltype(&ll_fz_drop_pixmap)> image(
            ll_fz_new_pixmap_with_bbox(ll_fz_device_rgb(), area, nullptr, 1), ll_fz_drop_pixmap);
        ll_fz_clear_pixmap(image.get());
        std::unique_ptr<fz_device, decltype(&ll_fz_drop_device)> device(
            ll_fz_new_draw_device(transform, image.get()), ll_fz_drop_device);
        ll_fz_run_page_contents(page.get(), device.get(), fz_matrix{1, 0, 0, 1, 0, 0}, nullptr);
        ll_fz_close_device(device.get());
        ll_fz_set_pixmap_resolution(image.get(), dpi, dpi);
        std::unique_ptr<fz_buffer, decltype(&ll_fz_drop_buffer)> png(
            ll_fz_new_buffer(4096), ll_fz_drop_buffer);
        std::unique_ptr<fz_output, decltype(&ll_fz_drop_output)> output(
            ll_fz_new_output_with_buffer(png.get()), ll_fz_drop_output);
        ll_fz_write_pixmap_as_png(output.get(), image.get());
        ll_fz_close_output(output.get());
        unsigned char* contents = nullptr;
        *size = ll_fz_buffer_storage(png.get(), &contents);
        *data = contents;
        origin[0] = area.x0;
        origin[1] = area.y0;
        *result = png.release();
        return 0;
    } catch (...) {
        return 1;
    }
}

__declspec(dllexport) void mc_mupdf_drop_png(fz_buffer* buffer) noexcept {
    ll_fz_drop_buffer(buffer);
}

}
