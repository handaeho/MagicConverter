#include <avif/avif.h>
#include <string.h>

#define API __declspec(dllexport)

typedef struct {
    uint32_t width, height, count, high_depth, alpha, orientation, metadata;
    int32_t repetitions;
} image_info;

typedef struct {
    const uint8_t *data;
    size_t len;
} metadata_block;

static uint32_t orientation(const avifImage *image)
{
    uint32_t rotation = image->transformFlags & AVIF_TRANSFORM_IROT ? image->irot.angle : 0;
    if (!(image->transformFlags & AVIF_TRANSFORM_IMIR)) {
        const uint32_t rotated[] = { 1, 8, 3, 6 };
        return rotated[rotation & 3];
    }
    const uint32_t mirrored[][2] = { { 4, 2 }, { 5, 7 }, { 2, 4 }, { 7, 5 } };
    return mirrored[rotation & 3][image->imir.axis & 1];
}

API avifResult magic_avif_open(const uint8_t *data, size_t size, uint32_t limit,
                               avifDecoder **output, image_info *info)
{
    avifDecoder *decoder = avifDecoderCreate();
    if (!decoder) return AVIF_RESULT_OUT_OF_MEMORY;
    decoder->codecChoice = AVIF_CODEC_CHOICE_DAV1D;
    decoder->maxThreads = 1;
    decoder->strictFlags &= ~(AVIF_STRICT_CLAP_VALID | AVIF_STRICT_PIXI_REQUIRED);
    decoder->imageSizeLimit = limit;
    decoder->imageCountLimit = 10000;
    avifResult result = avifDecoderSetIOMemory(decoder, data, size);
    if (result == AVIF_RESULT_OK) result = avifDecoderParse(decoder);
    if (result != AVIF_RESULT_OK) {
        avifDecoderDestroy(decoder);
        return result;
    }
    const avifImage *image = decoder->image;
    *info = (image_info){
        image->width, image->height, decoder->imageCount, image->depth > 8,
        decoder->alphaPresent, orientation(image),
        image->icc.size || image->exif.size || image->xmp.size, decoder->repetitionCount
    };
    *output = decoder;
    return AVIF_RESULT_OK;
}

API avifResult magic_avif_read(avifDecoder *decoder, uint8_t *pixels, size_t size,
                               double *milliseconds)
{
    avifResult result = avifDecoderNextImage(decoder);
    if (result != AVIF_RESULT_OK) return result;
    const avifImage *image = decoder->image;
    avifRGBImage rgb;
    avifRGBImageSetDefaults(&rgb, image);
    rgb.depth = 8;
    rgb.format = decoder->alphaPresent ? AVIF_RGB_FORMAT_RGBA : AVIF_RGB_FORMAT_RGB;
    rgb.rowBytes = rgb.width * (decoder->alphaPresent ? 4 : 3);
    if ((uint64_t)rgb.rowBytes * rgb.height != size) return AVIF_RESULT_INVALID_ARGUMENT;
    rgb.pixels = pixels;
    result = avifImageYUVToRGB(image, &rgb);
    *milliseconds = decoder->imageTiming.duration * 1000;
    return result;
}

API void magic_avif_close(avifDecoder *decoder) { avifDecoderDestroy(decoder); }

API void magic_avif_metadata(const avifDecoder *decoder, metadata_block output[3])
{
    const avifImage *image = decoder->image;
    output[0] = (metadata_block){ image->icc.data, image->icc.size };
    output[1] = (metadata_block){ image->exif.data, image->exif.size };
    output[2] = (metadata_block){ image->xmp.data, image->xmp.size };
}

API avifEncoder *magic_avif_encoder(int quality, int repetitions)
{
    avifEncoder *encoder = avifEncoderCreate();
    if (!encoder) return NULL;
    encoder->codecChoice = AVIF_CODEC_CHOICE_AOM;
    /* AOM changes its coding decisions between single-threaded and
       multithreaded encoding. Use the parallel path with a bounded pool. */
    encoder->maxThreads = 2;
    encoder->quality = quality;
    encoder->speed = 6;
    encoder->timescale = 1000;
    encoder->autoTiling = AVIF_TRUE;
    encoder->repetitionCount = repetitions;
    return encoder;
}

API avifResult magic_avif_add(avifEncoder *encoder, uint32_t width, uint32_t height,
                              const uint8_t *pixels, int alpha, uint64_t duration, int single)
{
    avifImage *image = avifImageCreate(width, height, 8, AVIF_PIXEL_FORMAT_YUV420);
    if (!image) return AVIF_RESULT_OUT_OF_MEMORY;
    image->yuvRange = AVIF_RANGE_FULL;
    image->colorPrimaries = AVIF_COLOR_PRIMARIES_BT709;
    image->transferCharacteristics = AVIF_TRANSFER_CHARACTERISTICS_SRGB;
    image->matrixCoefficients = AVIF_MATRIX_COEFFICIENTS_BT601;
    avifRGBImage rgb;
    avifRGBImageSetDefaults(&rgb, image);
    rgb.format = alpha ? AVIF_RGB_FORMAT_RGBA : AVIF_RGB_FORMAT_RGB;
    rgb.pixels = (uint8_t *)pixels;
    rgb.rowBytes = width * (alpha ? 4 : 3);
    avifResult result = avifImageRGBToYUV(image, &rgb);
    if (result == AVIF_RESULT_OK) {
        result = avifEncoderAddImage(encoder, image, duration,
                                     single ? AVIF_ADD_IMAGE_FLAG_SINGLE : AVIF_ADD_IMAGE_FLAG_NONE);
    }
    avifImageDestroy(image);
    return result;
}

API avifResult magic_avif_finish(avifEncoder *encoder, avifRWData *output)
{
    return avifEncoderFinish(encoder, output);
}

API void magic_avif_free(avifRWData *data) { avifRWDataFree(data); }
API void magic_avif_drop_encoder(avifEncoder *encoder) { avifEncoderDestroy(encoder); }
API const char *magic_avif_error(avifResult result) { return avifResultToString(result); }
