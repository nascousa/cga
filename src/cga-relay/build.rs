use std::env;
use std::fs;
use std::path::PathBuf;

const APP_ICON_GROUP_ID: u16 = 1;
const APP_ICON_SMALL_ID: u16 = 2;
const APP_ICON_LARGE_ID: u16 = 3;
const APP_ICON_GRAY_GROUP_ID: u16 = 4;
const APP_ICON_GRAY_SMALL_ID: u16 = 5;
const APP_ICON_GRAY_LARGE_ID: u16 = 6;
const APP_ICON_WARNING_GROUP_ID: u16 = 7;
const APP_ICON_WARNING_SMALL_ID: u16 = 8;
const APP_ICON_WARNING_LARGE_ID: u16 = 9;
const RT_ICON: u16 = 3;
const RT_GROUP_ICON: u16 = 14;

type IconResource<'a> = (u16, &'a [u8]);
type GroupIconEntry = (u8, u16, u32);
type GroupIconResource<'a> = (u16, &'a [GroupIconEntry]);

#[derive(Clone, Copy)]
enum IconVariant {
    Color,
    Gray,
    Warning,
}

fn main() {
    println!("cargo:rerun-if-changed=build.rs");
    if env::var("CARGO_CFG_TARGET_OS").as_deref() != Ok("windows") {
        return;
    }

    let out_dir = PathBuf::from(env::var_os("OUT_DIR").expect("OUT_DIR is set by Cargo"));
    let small = make_icon_image(16, IconVariant::Color);
    let large = make_icon_image(32, IconVariant::Color);
    let gray_small = make_icon_image(16, IconVariant::Gray);
    let gray_large = make_icon_image(32, IconVariant::Gray);
    let warning_small = make_icon_image(16, IconVariant::Warning);
    let warning_large = make_icon_image(32, IconVariant::Warning);
    let ico = make_ico(&[(16, &small), (32, &large)]);
    let color_group = [
        (16, APP_ICON_SMALL_ID, small.len() as u32),
        (32, APP_ICON_LARGE_ID, large.len() as u32),
    ];
    let gray_group = [
        (16, APP_ICON_GRAY_SMALL_ID, gray_small.len() as u32),
        (32, APP_ICON_GRAY_LARGE_ID, gray_large.len() as u32),
    ];
    let warning_group = [
        (16, APP_ICON_WARNING_SMALL_ID, warning_small.len() as u32),
        (32, APP_ICON_WARNING_LARGE_ID, warning_large.len() as u32),
    ];
    let res = make_res(
        &[
            (APP_ICON_SMALL_ID, &small),
            (APP_ICON_LARGE_ID, &large),
            (APP_ICON_GRAY_SMALL_ID, &gray_small),
            (APP_ICON_GRAY_LARGE_ID, &gray_large),
            (APP_ICON_WARNING_SMALL_ID, &warning_small),
            (APP_ICON_WARNING_LARGE_ID, &warning_large),
        ],
        &[
            (APP_ICON_GROUP_ID, &color_group[..]),
            (APP_ICON_GRAY_GROUP_ID, &gray_group[..]),
            (APP_ICON_WARNING_GROUP_ID, &warning_group[..]),
        ],
    );
    let ico_path = out_dir.join("cga-relay.ico");
    let res_path = out_dir.join("cga-relay.res");
    fs::write(&ico_path, ico).expect("generated ico should be writable");
    fs::write(&res_path, res).expect("generated res should be writable");

    if env::var("CARGO_CFG_TARGET_ENV").as_deref() == Ok("msvc") {
        println!("cargo:rustc-link-arg-bin=cga-relay={}", res_path.display());
    }
}

fn make_icon_image(size: u8, variant: IconVariant) -> Vec<u8> {
    let width = size as usize;
    let height = width;
    let mut image = Vec::new();
    write_u32(&mut image, 40);
    write_i32(&mut image, width as i32);
    write_i32(&mut image, (height * 2) as i32);
    write_u16(&mut image, 1);
    write_u16(&mut image, 32);
    write_u32(&mut image, 0);
    write_u32(&mut image, (width * height * 4) as u32);
    write_i32(&mut image, 0);
    write_i32(&mut image, 0);
    write_u32(&mut image, 0);
    write_u32(&mut image, 0);

    for y in (0..height).rev() {
        for x in 0..width {
            let (red, green, blue, alpha) = icon_pixel(x, y, width, height, variant);
            image.extend_from_slice(&[blue, green, red, alpha]);
        }
    }
    let mask_stride = width.div_ceil(32) * 4;
    let mask_offset = image.len();
    image.resize(mask_offset + mask_stride * height, 0);
    for row in 0..height {
        for x in 0..width {
            if image[40 + (row * width + x) * 4 + 3] == 0 {
                image[mask_offset + row * mask_stride + x / 8] |= 0x80 >> (x % 8);
            }
        }
    }
    image
}

fn icon_pixel(
    x: usize,
    y: usize,
    width: usize,
    height: usize,
    variant: IconVariant,
) -> (u8, u8, u8, u8) {
    // Sample fixed 16-unit lettering so both resource sizes share a smooth outline.
    const SAMPLES: usize = 4;
    let mut coverage = 0;
    for sy in 0..SAMPLES {
        for sx in 0..SAMPLES {
            let gx = (x as f64 + (sx as f64 + 0.5) / SAMPLES as f64) * 16.0 / width as f64;
            let gy = (y as f64 + (sy as f64 + 0.5) / SAMPLES as f64) * 16.0 / height as f64;
            if cga_pixel(gx, gy) {
                coverage += 1;
            }
        }
    }
    if coverage == 0 {
        return (0, 0, 0, 0);
    }
    let (red, green, blue) = match variant {
        IconVariant::Color => (34, 172, 82),
        IconVariant::Gray => (148, 148, 148),
        IconVariant::Warning => (242, 183, 5),
    };
    (
        red,
        green,
        blue,
        (coverage * 255 / (SAMPLES * SAMPLES)) as u8,
    )
}

fn cga_pixel(x: f64, y: f64) -> bool {
    if !(4.0..12.0).contains(&y) {
        return false;
    }
    if (1.0..5.0).contains(&x) {
        return open_bowl_pixel(x - 1.0, y) && !(x >= 3.0 && (6.0..10.0).contains(&y));
    }
    if (6.0..10.0).contains(&x) {
        let bowl = open_bowl_pixel(x - 6.0, y) && !(x >= 8.0 && (6.0..8.0).contains(&y));
        let bar = x >= 8.0 && (8.0..9.0).contains(&y);
        return bowl || bar;
    }
    if (11.0..15.0).contains(&x) {
        let local_x = x - 11.0;
        let inset = (12.0 - y) * 0.1875;
        let left = (inset..inset + 1.0).contains(&local_x);
        let right = (3.0 - inset..4.0 - inset).contains(&local_x);
        let bar = (9.0..10.0).contains(&y) && (inset..4.0 - inset).contains(&local_x);
        return left || right || bar;
    }
    false
}

fn open_bowl_pixel(x: f64, y: f64) -> bool {
    let dx = x - x.clamp(1.0, 3.0);
    let dy = y - y.clamp(5.0, 11.0);
    let outer = dx * dx + dy * dy <= 1.0;
    let inner = (1.0..3.0).contains(&x) && (5.0..11.0).contains(&y);
    outer && !inner
}

fn make_ico(images: &[(u8, &[u8])]) -> Vec<u8> {
    let mut out = Vec::new();
    write_u16(&mut out, 0);
    write_u16(&mut out, 1);
    write_u16(&mut out, images.len() as u16);
    let mut offset = 6 + images.len() * 16;
    for (size, data) in images {
        out.push(*size);
        out.push(*size);
        out.push(0);
        out.push(0);
        write_u16(&mut out, 1);
        write_u16(&mut out, 32);
        write_u32(&mut out, data.len() as u32);
        write_u32(&mut out, offset as u32);
        offset += data.len();
    }
    for (_, data) in images {
        out.extend_from_slice(data);
    }
    out
}

fn make_res(images: &[IconResource<'_>], groups: &[GroupIconResource<'_>]) -> Vec<u8> {
    let mut out = Vec::new();
    append_res_entry(&mut out, 0, 0, &[]);
    for (id, data) in images {
        append_res_entry(&mut out, RT_ICON, *id, data);
    }
    for (group_id, group_images) in groups {
        append_res_entry(
            &mut out,
            RT_GROUP_ICON,
            *group_id,
            &make_group_icon(group_images),
        );
    }
    out
}

fn make_group_icon(images: &[GroupIconEntry]) -> Vec<u8> {
    let mut out = Vec::new();
    write_u16(&mut out, 0);
    write_u16(&mut out, 1);
    write_u16(&mut out, images.len() as u16);
    for (size, id, bytes) in images {
        out.push(*size);
        out.push(*size);
        out.push(0);
        out.push(0);
        write_u16(&mut out, 1);
        write_u16(&mut out, 32);
        write_u32(&mut out, *bytes);
        write_u16(&mut out, *id);
    }
    out
}

fn append_res_entry(out: &mut Vec<u8>, type_id: u16, name_id: u16, data: &[u8]) {
    align4(out);
    let start = out.len();
    write_u32(out, data.len() as u32);
    write_u32(out, 0);
    write_numeric_resource_id(out, type_id);
    write_numeric_resource_id(out, name_id);
    align4(out);
    write_u32(out, 0);
    write_u16(out, 0x0030);
    write_u16(out, 0x0409);
    write_u32(out, 0);
    write_u32(out, 0);
    let header_size = (out.len() - start) as u32;
    out[start + 4..start + 8].copy_from_slice(&header_size.to_le_bytes());
    out.extend_from_slice(data);
    align4(out);
}

fn write_numeric_resource_id(out: &mut Vec<u8>, id: u16) {
    write_u16(out, 0xffff);
    write_u16(out, id);
}

fn align4(out: &mut Vec<u8>) {
    while !out.len().is_multiple_of(4) {
        out.push(0);
    }
}

fn write_u16(out: &mut Vec<u8>, value: u16) {
    out.extend_from_slice(&value.to_le_bytes());
}

fn write_u32(out: &mut Vec<u8>, value: u32) {
    out.extend_from_slice(&value.to_le_bytes());
}

fn write_i32(out: &mut Vec<u8>, value: i32) {
    out.extend_from_slice(&value.to_le_bytes());
}
