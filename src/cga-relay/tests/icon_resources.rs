include!("../build.rs");

#[test]
fn icon_is_only_cga_on_transparent_background() {
    for size in [16, 32] {
        for variant in [IconVariant::Color, IconVariant::Gray, IconVariant::Warning] {
            let scale = size / 16;
            let alpha = |x, y| icon_pixel(x * scale, y * scale, size, size, variant).3;
            assert_eq!(alpha(1, 7), 255, "C stem");
            assert_eq!(alpha(2, 4), 255, "C top");
            assert_eq!(alpha(2, 11), 255, "C bottom");
            assert_eq!(alpha(3, 8), 0, "C opening");
            assert_eq!(alpha(6, 7), 255, "G stem");
            assert_eq!(alpha(8, 8), 255, "G crossbar");
            assert_eq!(alpha(9, 7), 0, "G opening");
            assert_eq!(alpha(7, 7), 0, "G counter");
            assert!(alpha(13, 4) > 0, "A apex");
            assert_eq!(alpha(13, 9), 255, "A crossbar");
            assert!(alpha(11, 11) > 0, "A left leg");
            assert!(alpha(14, 11) > 0, "A right leg");
            assert!(alpha(13, 11) < 128, "space between A legs");
            for y in 0..size {
                for x in [5 * scale, 10 * scale] {
                    for dx in 0..scale {
                        assert_eq!(
                            icon_pixel(x + dx, y, size, size, variant).3,
                            0,
                            "letter spacing"
                        );
                    }
                }
            }
            for p in 0..size {
                for (x, y) in [(p, 0), (p, size - 1), (0, p), (size - 1, p)] {
                    assert_eq!(icon_pixel(x, y, size, size, variant), (0, 0, 0, 0));
                }
            }
            assert!((0..size).any(|y| (0..size).any(|x| {
                let a = icon_pixel(x, y, size, size, variant).3;
                a > 0 && a < 255
            })));
        }
    }
}

#[test]
fn a_counter_stays_open_above_the_crossbar() {
    assert!(!cga_pixel(13.0, 8.0));
    assert!(cga_pixel(13.0, 9.5));
}

#[test]
fn status_variants_change_color_without_changing_the_glyph() {
    for size in [16, 32] {
        for y in 0..size {
            for x in 0..size {
                let color = icon_pixel(x, y, size, size, IconVariant::Color);
                let gray = icon_pixel(x, y, size, size, IconVariant::Gray);
                let warning = icon_pixel(x, y, size, size, IconVariant::Warning);
                assert_eq!(color.3, gray.3);
                assert_eq!(color.3, warning.3);
                if color.3 > 0 {
                    assert_eq!((color.0, color.1, color.2), (34, 172, 82));
                    assert_eq!((gray.0, gray.1, gray.2), (148, 148, 148));
                    assert_eq!((warning.0, warning.1, warning.2), (242, 183, 5));
                }
            }
        }
    }
}

#[test]
fn dib_pixels_and_transparency_mask_match_the_glyph() {
    for size in [16u8, 32] {
        for variant in [IconVariant::Color, IconVariant::Gray, IconVariant::Warning] {
            let image = make_icon_image(size, variant);
            let width = size as usize;
            let stride = width.div_ceil(32) * 4;
            let mask_offset = 40 + width * width * 4;
            assert_eq!(image.len(), mask_offset + stride * width);
            assert_eq!(&image[4..8], &(width as i32).to_le_bytes());
            assert_eq!(&image[8..12], &((width * 2) as i32).to_le_bytes());
            for row in 0..width {
                for x in 0..width {
                    let (r, g, b, a) = icon_pixel(x, width - 1 - row, width, width, variant);
                    let offset = 40 + (row * width + x) * 4;
                    assert_eq!(&image[offset..offset + 4], &[b, g, r, a]);
                    let masked = image[mask_offset + row * stride + x / 8] & (0x80 >> (x % 8));
                    assert_eq!(masked != 0, a == 0);
                }
            }
        }
    }
}
