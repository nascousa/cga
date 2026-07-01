use std::env;
use std::fs;
use std::path::PathBuf;

const APP_ICON_GROUP_ID: u16 = 1;
const APP_ICON_SMALL_ID: u16 = 2;
const APP_ICON_LARGE_ID: u16 = 3;
const APP_ICON_GRAY_GROUP_ID: u16 = 4;
const APP_ICON_GRAY_SMALL_ID: u16 = 5;
const APP_ICON_GRAY_LARGE_ID: u16 = 6;
const RT_ICON: u16 = 3;
const RT_GROUP_ICON: u16 = 14;

#[derive(Clone, Copy)]
enum IconVariant {
    Color,
    Gray,
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
    let ico = make_ico(&[(16, &small), (32, &large)]);
    let color_group = [
        (16, APP_ICON_SMALL_ID, small.len() as u32),
        (32, APP_ICON_LARGE_ID, large.len() as u32),
    ];
    let gray_group = [
        (16, APP_ICON_GRAY_SMALL_ID, gray_small.len() as u32),
        (32, APP_ICON_GRAY_LARGE_ID, gray_large.len() as u32),
    ];
    let res = make_res(
        &[
            (APP_ICON_SMALL_ID, &small),
            (APP_ICON_LARGE_ID, &large),
            (APP_ICON_GRAY_SMALL_ID, &gray_small),
            (APP_ICON_GRAY_LARGE_ID, &gray_large),
        ],
        &[
            (APP_ICON_GROUP_ID, &color_group[..]),
            (APP_ICON_GRAY_GROUP_ID, &gray_group[..]),
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
    let mask_stride = ((width + 31) / 32) * 4;
    image.resize(image.len() + mask_stride * height, 0);
    image
}

fn icon_pixel(
    x: usize,
    y: usize,
    width: usize,
    height: usize,
    variant: IconVariant,
) -> (u8, u8, u8, u8) {
    let margin = width / 12;
    let radius = width / 5;
    if !inside_rounded_rect(x, y, width, height, margin, radius) {
        return apply_icon_variant((0, 0, 0, 0), variant);
    }

    let mut color = (15, 132, 62, 255);
    if x + y > width + width / 5 {
        color = (34, 172, 82, 255);
    }

    if letter_r_pixel(x, y, width, height) {
        return apply_icon_variant((245, 255, 250, 255), variant);
    }

    apply_icon_variant(color, variant)
}

fn letter_r_pixel(x: usize, y: usize, width: usize, height: usize) -> bool {
    let stroke = (width / 6).max(2);
    let left = width / 4;
    let right = width * 3 / 4;
    let top = height / 5;
    let mid = height / 2;
    let bottom = height * 4 / 5;
    let vertical = x >= left && x < left + stroke && y >= top && y <= bottom;
    let top_bar = x >= left && x <= right && y >= top && y < top + stroke;
    let middle_bar =
        x >= left && x <= right && y >= mid.saturating_sub(stroke / 2) && y < mid + stroke;
    let bowl_right = x + stroke > right && x <= right && y >= top && y <= mid;
    let leg_offset = y.saturating_sub(mid);
    let diagonal_start = left + stroke + leg_offset / 2;
    let diagonal_leg = y > mid && y <= bottom && x >= diagonal_start && x < diagonal_start + stroke;

    vertical || top_bar || middle_bar || bowl_right || diagonal_leg
}

fn apply_icon_variant(color: (u8, u8, u8, u8), variant: IconVariant) -> (u8, u8, u8, u8) {
    let (red, green, blue, alpha) = color;
    match variant {
        IconVariant::Color => color,
        IconVariant::Gray => {
            if alpha == 0 {
                color
            } else {
                let gray = ((red as u16 * 30 + green as u16 * 59 + blue as u16 * 11) / 100) as u8;
                (gray, gray, gray, alpha)
            }
        }
    }
}

fn inside_rounded_rect(
    x: usize,
    y: usize,
    width: usize,
    height: usize,
    margin: usize,
    radius: usize,
) -> bool {
    if x < margin || y < margin || x >= width - margin || y >= height - margin {
        return false;
    }
    let left = margin + radius;
    let right = width - margin - radius - 1;
    let top = margin + radius;
    let bottom = height - margin - radius - 1;
    if (x >= left && x <= right) || (y >= top && y <= bottom) {
        return true;
    }
    let center_x = if x < left { left } else { right };
    let center_y = if y < top { top } else { bottom };
    inside_circle(x, y, center_x, center_y, radius)
}

fn inside_circle(x: usize, y: usize, center_x: usize, center_y: usize, radius: usize) -> bool {
    let dx = x as isize - center_x as isize;
    let dy = y as isize - center_y as isize;
    dx * dx + dy * dy <= (radius * radius) as isize
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

fn make_res(images: &[(u16, &[u8])], groups: &[(u16, &[(u8, u16, u32)])]) -> Vec<u8> {
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

fn make_group_icon(images: &[(u8, u16, u32)]) -> Vec<u8> {
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
    while out.len() % 4 != 0 {
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
